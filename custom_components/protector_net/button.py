import logging
from urllib.parse import urlparse

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .device import ProtectorNetDevice
from . import api
from .const import DEFAULT_OVERRIDE_MINUTES, KEY_PLAN_IDS, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback
) -> None:
    """
    Set up Protector.Net door and action-plan buttons based on user selections.
    """
    # Host used for unique_ids (sanitized); real host & hub id come from __init__.py
    host_safe = (urlparse(entry.data["base_url"]).hostname or "").replace(":", "_")

    # Fetch doors & provision our HA log plan
    try:
        doors = await api.get_all_doors(hass, entry.entry_id)
    except Exception as e:
        _LOGGER.error("Failed to fetch doors from Protector.Net: %s", e)
        return

    try:
        log_plan_id = await api.find_or_create_ha_log_plan(hass, entry.entry_id)
        hass.data[DOMAIN][entry.entry_id]["ha_log_plan_id"] = log_plan_id
        _LOGGER.debug("HA log plan id is %s", log_plan_id)
    except Exception as e:
        _LOGGER.error("Failed to create/find HA log plan: %s", e)
        hass.data[DOMAIN][entry.entry_id]["ha_log_plan_id"] = None

    selected = entry.options.get("entities", entry.data.get("entities", []))
    override_mins = entry.options.get(
        "override_minutes",
        entry.data.get("override_minutes", DEFAULT_OVERRIDE_MINUTES)
    )

    raw_plan_ids = entry.options.get(KEY_PLAN_IDS, entry.data.get(KEY_PLAN_IDS, []))
    plan_ids = [int(pid) for pid in raw_plan_ids]

    system_ids = []
    for trig_id in plan_ids:
        try:
            sys_id = await api.find_or_clone_system_plan(hass, entry.entry_id, trig_id)
            system_ids.append(sys_id)
        except Exception as e:
            _LOGGER.error("Error cloning trigger plan %s: %s", trig_id, e)
    _LOGGER.debug("Protector Net: system plan ids=%s", system_ids)

    entities: list[ButtonEntity] = []

    # Door buttons
    for door in doors:
        if "_pulse_unlock" in selected:
            entities.append(DoorPulseUnlockButton(hass, entry, door, host_safe))
        if "_resume_schedule" in selected:
            entities.append(DoorResumeScheduleButton(hass, entry, door, host_safe))
        if "_unlock_until_resume" in selected:
            entities.append(DoorOverrideUntilResumeButton(hass, entry, door, host_safe))
        if "_override_card_or_pin" in selected:
            entities.append(DoorOverrideUntilResumeCardOrPinButton(hass, entry, door, host_safe))
        if "_unlock_until_next_schedule" in selected:
            entities.append(DoorOverrideUntilNextScheduleButton(hass, entry, door, host_safe))
        if "_timed_override_unlock" in selected:
            entities.append(DoorTimedOverrideUnlockButton(hass, entry, door, host_safe, override_mins))

    # Action Plan buttons (System clones)
    if system_ids:
        try:
            plans = await api.get_action_plans(hass, entry.entry_id)
        except Exception as e:
            _LOGGER.error("Failed to fetch action plans: %s", e)
            plans = []

        for plan in plans:
            if plan["Id"] in system_ids:
                entities.append(ActionPlanButton(hass, entry, plan, host_safe))

    async_add_entities(entities)


class BaseDoorButton(ProtectorNetDevice, ButtonEntity):
    """Base class for door buttons—handles per-door device_info & entry scoping."""
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, door: dict, host_safe: str):
        super().__init__(entry)
        self.hass = hass
        self._entry = entry
        self._entry_id = entry.entry_id
        self._door = door
        self.door_id = door["Id"]
        self.door_name = door.get("Name", "Unknown Door")

        # Values prepared in __init__.py (async_setup_entry) for this entry:
        entry_data = hass.data[DOMAIN][entry.entry_id]
        # raw host (e.g. "host:port") used to match hub identifier; do not sanitize here
        self._host_key: str = entry_data.get("host") or (urlparse(entry.data["base_url"]).netloc or "")
        self._hub_identifier: str = entry_data.get("hub_identifier", f"hub:{self._host_key}|{self._entry_id}")

        # For unique_id readability we keep the sanitized host
        self._host_safe = host_safe

    @property
    def device_info(self):
        """One device per door; nested under the entry's hub device."""
        return {
            "identifiers": {(DOMAIN, f"door:{self._host_key}:{self.door_id}|{self._entry_id}")},
            "name": self.door_name,
            "manufacturer": "Hartmann Controls",
            "model": "Protector.Net Door",
            "via_device": (DOMAIN, self._hub_identifier),
            "configuration_url": self._entry.data.get("base_url"),
        }


class DoorPulseUnlockButton(BaseDoorButton):
    def __init__(self, hass, entry, door, host_safe: str):
        super().__init__(hass, entry, door, host_safe)
        self._attr_name = "Pulse Unlock"
        self._attr_unique_id = f"protector_net_{self._host_safe}_{self._entry_id}_{self.door_id}_pulse_unlock"

    async def async_press(self):
        # 1) normal pulse unlock
        await api.pulse_unlock(self.hass, self._entry_id, [self.door_id])

        # 2) fire HA Door Log
        plan_id = self.hass.data[DOMAIN][self._entry_id].get("ha_log_plan_id")
        _LOGGER.debug("Calling HA Door Log plan %s", plan_id)
        if not plan_id:
            return

        await api.execute_action_plan(
            self.hass,
            self._entry_id,
            plan_id,
            variables={
                "App":  "Home Assistant",
                "Door": self.door_name
            }
        )


class DoorResumeScheduleButton(BaseDoorButton):
    def __init__(self, hass, entry, door, host_safe: str):
        super().__init__(hass, entry, door, host_safe)
        self._attr_name = "Resume Schedule"
        self._attr_unique_id = f"protector_net_{self._host_safe}_{self._entry_id}_{self.door_id}_resume_schedule"

    async def async_press(self):
        await api.resume_schedule(self.hass, self._entry_id, [self.door_id])


class DoorOverrideUntilResumeButton(BaseDoorButton):
    def __init__(self, hass, entry, door, host_safe: str):
        super().__init__(hass, entry, door, host_safe)
        self._attr_name = "Unlock Until Resume"
        self._attr_unique_id = f"protector_net_{self._host_safe}_{self._entry_id}_{self.door_id}_unlock_until_resume"

    async def async_press(self):
        await api.set_override(self.hass, self._entry_id, [self.door_id], "Resume")
        plan_id = self.hass.data[DOMAIN][self._entry_id].get("ha_log_plan_id")
        if not plan_id:
            return

        await api.execute_action_plan(
            self.hass,
            self._entry_id,
            plan_id,
            variables={
                "App":  "Home Assistant",
                "Door": self.door_name
            }
        )


class DoorOverrideUntilResumeCardOrPinButton(BaseDoorButton):
    def __init__(self, hass, entry, door, host_safe: str):
        super().__init__(hass, entry, door, host_safe)
        self._attr_name = "CardOrPin Until Resume"
        self._attr_unique_id = f"protector_net_{self._host_safe}_{self._entry_id}_{self.door_id}_cardorpin_until_resume"

    async def async_press(self):
        await api.override_until_resume_card_or_pin(self.hass, self._entry_id, [self.door_id])


class DoorOverrideUntilNextScheduleButton(BaseDoorButton):
    def __init__(self, hass, entry, door, host_safe: str):
        super().__init__(hass, entry, door, host_safe)
        self._attr_name = "Unlock Until Next Schedule"
        self._attr_unique_id = f"protector_net_{self._host_safe}_{self._entry_id}_{self.door_id}_unlock_until_next_schedule"

    async def async_press(self):
        await api.set_override(self.hass, self._entry_id, [self.door_id], "Schedule")
        plan_id = self.hass.data[DOMAIN][self._entry_id].get("ha_log_plan_id")
        if not plan_id:
            return

        await api.execute_action_plan(
            self.hass,
            self._entry_id,
            plan_id,
            variables={
                "App":  "Home Assistant",
                "Door": self.door_name
            }
        )


class DoorTimedOverrideUnlockButton(BaseDoorButton):
    def __init__(self, hass, entry, door, host_safe: str, minutes: int):
        super().__init__(hass, entry, door, host_safe)
        self._override_minutes = minutes
        self._attr_name = "Timed Override Unlock"
        self._attr_unique_id = f"protector_net_{self._host_safe}_{self._entry_id}_{self.door_id}_timed_override_unlock"

    async def async_press(self):
        await api.set_override(
            self.hass,
            self._entry_id,
            [self.door_id],
            "Time",
            minutes=self._override_minutes
        )
        plan_id = self.hass.data[DOMAIN][self._entry_id].get("ha_log_plan_id")
        if not plan_id:
            return

        await api.execute_action_plan(
            self.hass,
            self._entry_id,
            plan_id,
            variables={
                "App":  "Home Assistant",
                "Door": self.door_name
            }
        )


class ActionPlanButton(ProtectorNetDevice, ButtonEntity):
    """A button to execute a Protector.Net action plan (attached to the hub device)."""
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, plan: dict, host_safe: str):
        super().__init__(entry)
        self.hass = hass
        self._entry = entry
        self._entry_id = entry.entry_id
        self._plan = plan
        self._host_safe = host_safe

        # From __init__.py entry_data
        entry_data = hass.data[DOMAIN][entry.entry_id]
        self._host_key: str = entry_data.get("host") or (urlparse(entry.data["base_url"]).netloc or "")
        self._hub_identifier: str = entry_data.get("hub_identifier", f"hub:{self._host_key}|{self._entry_id}")

        self._attr_name = f"Action Plan: {plan['Name']}"
        self._attr_unique_id = f"protector_net_{self._host_safe}_{self._entry_id}_action_plan_{plan['Id']}"

    @property
    def device_info(self):
        """Attach plan buttons to the hub device for this entry."""
        return {
            "identifiers": {(DOMAIN, self._hub_identifier)},  # same device as the hub
            "manufacturer": "Hartmann Controls",
            "model": "Protector.Net",
            "name": f"Protector.Net ({self._host_key})",
            "configuration_url": self._entry.data.get("base_url"),
        }

    async def async_press(self) -> None:
        success = await api.execute_action_plan(self.hass, self._entry_id, self._plan["Id"])
        if not success:
            _LOGGER.error("Failed to execute action plan %s", self._plan["Id"])

# custom_components/protector_net/button.py
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
    host = urlparse(entry.data["base_url"]).hostname.replace(":", "_")

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

    entities = []

    # Door buttons
    for door in doors:
        if "_pulse_unlock" in selected:
            entities.append(DoorPulseUnlockButton(hass, entry, door))
        if "_resume_schedule" in selected:
            entities.append(DoorResumeScheduleButton(hass, entry, door))
        if "_unlock_until_resume" in selected:
            entities.append(DoorOverrideUntilResumeButton(hass, entry, door))
        if "_override_card_or_pin" in selected:
            entities.append(DoorOverrideUntilResumeCardOrPinButton(hass, entry, door))
        if "_unlock_until_next_schedule" in selected:
            entities.append(DoorOverrideUntilNextScheduleButton(hass, entry, door))
        if "_timed_override_unlock" in selected:
            entities.append(DoorTimedOverrideUnlockButton(hass, entry, door, override_mins))

    # Action Plan buttons (System clones)
    if system_ids:
        try:
            plans = await api.get_action_plans(hass, entry.entry_id)
        except Exception as e:
            _LOGGER.error("Failed to fetch action plans: %s", e)
            plans = []

        for plan in plans:
            if plan["Id"] in system_ids:
                entities.append(ActionPlanButton(hass, entry, plan))

    async_add_entities(entities)


class BaseDoorButton(ProtectorNetDevice, ButtonEntity):
    """Base class for door buttons—handles unique device_info & entry_id."""
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, door: dict):
        super().__init__(entry)
        self._entry_id = entry.entry_id
        self.hass = hass
        self._door = door
        self.door_id = door["Id"]
        self.door_name = door.get("Name", "Unknown Door")
        self._host = urlparse(entry.data["base_url"]).hostname.replace(":", "_")


class DoorPulseUnlockButton(BaseDoorButton):
    def __init__(self, hass, entry, door):
        super().__init__(hass, entry, door)
        self._attr_name = f"{self.door_name} Pulse Unlock"
        self._attr_unique_id = f"protector_net_{self._host}_{self.door_id}_pulse_unlock"

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
    def __init__(self, hass, entry, door):
        super().__init__(hass, entry, door)
        self._attr_name = f"{self.door_name} Resume Schedule"
        self._attr_unique_id = f"protector_net_{self._host}_{self.door_id}_resume_schedule"

    async def async_press(self):
        await api.resume_schedule(self.hass, self._entry_id, [self.door_id])


class DoorOverrideUntilResumeButton(BaseDoorButton):
    def __init__(self, hass, entry, door):
        super().__init__(hass, entry, door)
        self._attr_name = f"{self.door_name} Unlock Until Resume"
        self._attr_unique_id = f"protector_net_{self._host}_{self.door_id}_unlock_until_resume"

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
    def __init__(self, hass, entry, door):
        super().__init__(hass, entry, door)
        self._attr_name = f"{self.door_name} CardOrPin Until Resume"
        self._attr_unique_id = f"protector_net_{self._host}_{self.door_id}_cardorpin_until_resume"

    async def async_press(self):
        await api.override_until_resume_card_or_pin(self.hass, self._entry_id, [self.door_id])


class DoorOverrideUntilNextScheduleButton(BaseDoorButton):
    def __init__(self, hass, entry, door):
        super().__init__(hass, entry, door)
        self._attr_name = f"{self.door_name} Unlock Until Next Schedule"
        self._attr_unique_id = f"protector_net_{self._host}_{self.door_id}_unlock_until_next_schedule"

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
    def __init__(self, hass, entry, door, minutes: int):
        super().__init__(hass, entry, door)
        self._override_minutes = minutes
        self._attr_name = f"{self.door_name} Timed Override Unlock"
        self._attr_unique_id = f"protector_net_{self._host}_{self.door_id}_timed_override_unlock"

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
    """A button to execute a Protector.Net action plan."""
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, plan: dict):
        super().__init__(entry)
        self._entry_id = entry.entry_id
        self.hass = hass
        self._plan = plan
        self._host = urlparse(entry.data["base_url"]).hostname.replace(":", "_")
        self._attr_name = f"Action Plan: {plan['Name']}"
        self._attr_unique_id = f"protector_net_{self._host}_action_plan_{plan['Id']}"

    async def async_press(self) -> None:
        success = await api.execute_action_plan(self.hass, self._entry_id, self._plan["Id"])
        if not success:
            _LOGGER.error("Failed to execute action plan %s", self._plan["Id"])

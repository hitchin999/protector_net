# custom_components/protector_net/button.py
import logging
from urllib.parse import urlparse

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .device import ProtectorNetDevice
from . import api
from .const import DEFAULT_OVERRIDE_MINUTES, KEY_PLAN_IDS

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback
) -> None:
    """
    Set up Protector.Net door and action-plan buttons based on user selections.
    """
    # Determine a host-based namespace for uniqueness
    host = urlparse(entry.data["base_url"]).netloc.replace(":", "_")

    try:
        doors = await api.get_all_doors(hass)
    except Exception as e:
        _LOGGER.error("Failed to fetch doors from Protector.Net: %s", e)
        return

    selected = entry.options.get("entities", entry.data.get("entities", []))
    override_mins = entry.options.get(
        "override_minutes",
        entry.data.get("override_minutes", DEFAULT_OVERRIDE_MINUTES)
    )
    # load plan_ids (they may be strings if coming from options), convert to ints
    raw_plan_ids = entry.options.get(KEY_PLAN_IDS, entry.data.get(KEY_PLAN_IDS, []))
    plan_ids = [int(pid) for pid in raw_plan_ids]
    _LOGGER.debug(
        "PROTECTOR_NET: selected door entities=%s plan_ids=%s", selected, plan_ids
    )

    entities = []
    # Door buttons
    for door in doors:
        if "_pulse_unlock" in selected:
            entities.append(DoorPulseUnlockButton(hass, host, door))
        if "_resume_schedule" in selected:
            entities.append(DoorResumeScheduleButton(hass, host, door))
        if "_unlock_until_resume" in selected:
            entities.append(DoorOverrideUntilResumeButton(hass, host, door))
        if "_override_card_or_pin" in selected:
            entities.append(DoorOverrideUntilResumeCardOrPinButton(hass, host, door))
        if "_unlock_until_next_schedule" in selected:
            entities.append(DoorOverrideUntilNextScheduleButton(hass, host, door))
        if "_timed_override_unlock" in selected:
            entities.append(DoorTimedOverrideUnlockButton(hass, host, door, override_mins))

    # Action Plan buttons
    if plan_ids:
        plans = await api.get_action_plans(
            hass,
            entry.data["base_url"],
            entry.data["session_cookie"],
            entry.data["partition_id"],
        )
        for plan in plans:
            if plan["Id"] in plan_ids:
                entities.append(ActionPlanButton(hass, host, plan))

    async_add_entities(entities)


class BaseDoorButton(ProtectorNetDevice, ButtonEntity):
    def __init__(self, hass: HomeAssistant, door: dict):
        # Avoid MRO issues by not calling super().__init__ directly
        self.hass = hass
        self._door = door
        self.door_id = door["Id"]
        self.door_name = door.get("Name", "Unknown Door")


class DoorPulseUnlockButton(BaseDoorButton):
    def __init__(self, hass: HomeAssistant, host: str, door: dict):
        super().__init__(hass, door)
        self._attr_name = f"{self.door_name} Pulse Unlock"
        self._attr_unique_id = f"protector_net_{host}_{self.door_id}_pulse_unlock"

    async def async_press(self):
        await api.pulse_unlock(self.hass, [self.door_id])


class DoorResumeScheduleButton(BaseDoorButton):
    def __init__(self, hass: HomeAssistant, host: str, door: dict):
        super().__init__(hass, door)
        self._attr_name = f"{self.door_name} Resume Schedule"
        self._attr_unique_id = f"protector_net_{host}_{self.door_id}_resume_schedule"

    async def async_press(self):
        await api.resume_schedule(self.hass, [self.door_id])


class DoorOverrideUntilResumeButton(BaseDoorButton):
    def __init__(self, hass: HomeAssistant, host: str, door: dict):
        super().__init__(hass, door)
        self._attr_name = f"{self.door_name} Unlock Until Resume"
        self._attr_unique_id = f"protector_net_{host}_{self.door_id}_unlock_until_resume"

    async def async_press(self):
        await api.set_override(self.hass, [self.door_id], "Resume")


class DoorOverrideUntilResumeCardOrPinButton(BaseDoorButton):
    def __init__(self, hass: HomeAssistant, host: str, door: dict):
        super().__init__(hass, door)
        self._attr_name = f"{self.door_name} CardOrPin Until Resume"
        self._attr_unique_id = f"protector_net_{host}_{self.door_id}_cardorpin_until_resume"

    async def async_press(self):
        await api.override_until_resume_card_or_pin(self.hass, [self.door_id])


class DoorOverrideUntilNextScheduleButton(BaseDoorButton):
    def __init__(self, hass: HomeAssistant, host: str, door: dict):
        super().__init__(hass, door)
        self._attr_name = f"{self.door_name} Unlock Until Next Schedule"
        self._attr_unique_id = f"protector_net_{host}_{self.door_id}_unlock_until_next_schedule"

    async def async_press(self):
        await api.set_override(self.hass, [self.door_id], "Schedule")


class DoorTimedOverrideUnlockButton(BaseDoorButton):
    def __init__(self, hass: HomeAssistant, host: str, door: dict, minutes: int):
        super().__init__(hass, door)
        self._override_minutes = minutes
        self._attr_name = f"{self.door_name} Timed Override Unlock"
        self._attr_unique_id = f"protector_net_{host}_{self.door_id}_timed_override_unlock"

    async def async_press(self):
        await api.set_override(
            self.hass,
            [self.door_id],
            "Time",
            minutes=self._override_minutes
        )


class ActionPlanButton(ButtonEntity):
    """A button to execute a Protector.Net action plan."""

    def __init__(self, hass: HomeAssistant, host: str, plan: dict):
        self.hass = hass
        self._plan = plan

        self._attr_name = f"Action Plan: {plan['Name']}"
        self._attr_unique_id = f"protector_net_{host}_action_plan_{plan['Id']}"

    async def async_press(self) -> None:
        success = await api.execute_action_plan(
            self.hass,
            self._plan["BaseUrl"] if "BaseUrl" in self._plan else None,
            self._plan["SessionCookie"] if "SessionCookie" in self._plan else None,
            self._plan["Id"],
        )
        if not success:
            _LOGGER.error("Failed to execute action plan %s", self._plan["Id"])

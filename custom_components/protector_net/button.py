# custom_components/protector_net/button.py
import logging
from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .device import ProtectorNetDevice
from . import api
from .const import DOMAIN, DEFAULT_OVERRIDE_MINUTES

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback
) -> None:
    """
    Set up Protector.Net door buttons based on user-selected entities and override minutes.
    """
    try:
        doors = await api.get_all_doors(hass)
    except Exception as e:
        _LOGGER.error("Failed to fetch doors from Protector.Net: %s", e)
        return

    selected = entry.options.get("entities", entry.data.get("entities", []))
    override_mins = entry.options.get(
        "override_minutes", entry.data.get("override_minutes", DEFAULT_OVERRIDE_MINUTES)
    )

    entities = []
    for door in doors:
        if "_pulse_unlock" in selected:
            entities.append(DoorPulseUnlockButton(hass, door))
        if "_resume_schedule" in selected:
            entities.append(DoorResumeScheduleButton(hass, door))
        if "_unlock_until_resume" in selected:
            entities.append(DoorOverrideUntilResumeButton(hass, door))
        if "_override_card_or_pin" in selected:
            entities.append(DoorOverrideUntilResumeCardOrPinButton(hass, door))
        if "_unlock_until_next_schedule" in selected:
            entities.append(DoorOverrideUntilNextScheduleButton(hass, door))
        if "_timed_override_unlock" in selected:
            entities.append(DoorTimedOverrideUnlockButton(hass, door, override_mins))

    async_add_entities(entities)


class BaseDoorButton(ProtectorNetDevice, ButtonEntity):
    def __init__(self, hass: HomeAssistant, door: dict):
        # Store references without calling super().__init__ to avoid MRO issues
        self.hass = hass
        self._door = door
        self.door_id = door["Id"]
        self.door_name = door.get("Name", "Unknown Door")


class DoorPulseUnlockButton(BaseDoorButton):
    def __init__(self, hass: HomeAssistant, door: dict):
        super().__init__(hass, door)
        self._attr_name = f"{self.door_name} Pulse Unlock"
        self._attr_unique_id = f":protector_net_{self.door_id}_pulse_unlock"

    async def async_press(self):
        await api.pulse_unlock(self.hass, [self.door_id])


class DoorResumeScheduleButton(BaseDoorButton):
    def __init__(self, hass: HomeAssistant, door: dict):
        super().__init__(hass, door)
        self._attr_name = f"{self.door_name} Resume Schedule"
        self._attr_unique_id = f"protector_net_{self.door_id}_resume_schedule"

    async def async_press(self):
        await api.resume_schedule(self.hass, [self.door_id])


class DoorOverrideUntilResumeButton(BaseDoorButton):
    def __init__(self, hass: HomeAssistant, door: dict):
        super().__init__(hass, door)
        self._attr_name = f"{self.door_name} Unlock Until Resume"
        self._attr_unique_id = f"protector_net_{self.door_id}_unlock_until_resume"

    async def async_press(self):
        await api.set_override(self.hass, [self.door_id], "Resume")


class DoorOverrideUntilResumeCardOrPinButton(BaseDoorButton):
    def __init__(self, hass: HomeAssistant, door: dict):
        super().__init__(hass, door)
        self._attr_name = f"{self.door_name} CardOrPin Until Resume"
        self._attr_unique_id = f"protector_net_{self.door_id}_cardorpin_until_resume"

    async def async_press(self):
        await api.override_until_resume_card_or_pin(self.hass, [self.door_id])


class DoorOverrideUntilNextScheduleButton(BaseDoorButton):
    def __init__(self, hass: HomeAssistant, door: dict):
        super().__init__(hass, door)
        self._attr_name = f"{self.door_name} Unlock Until Next Schedule"
        self._attr_unique_id = f"protector_net_{self.door_id}_unlock_until_next_schedule"

    async def async_press(self):
        await api.set_override(self.hass, [self.door_id], "Schedule")


class DoorTimedOverrideUnlockButton(BaseDoorButton):
    def __init__(self, hass: HomeAssistant, door: dict, minutes: int):
        super().__init__(hass, door)
        self._override_minutes = minutes
        self._attr_name = f"{self.door_name} Timed Override Unlock"
        self._attr_unique_id = f"protector_net_{self.door_id}_timed_override_unlock"

    async def async_press(self):
        await api.set_override(
            self.hass,
            [self.door_id],
            "Time",
            minutes=self._override_minutes
        )

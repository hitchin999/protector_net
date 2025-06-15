# custom_components/protector_net/button.py
import logging
from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .device import ProtectorNetDevice

from . import api
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback
) -> None:
    try:
        doors = await api.get_all_doors(hass)
    except Exception as e:
        _LOGGER.error("Failed to fetch doors from Protector.Net: %s", e)
        return

    entities = []
    for door in doors:
        entities.append(DoorPulseUnlockButton(hass, door))
        entities.append(DoorResumeScheduleButton(hass, door))
        entities.append(DoorOverrideUntilResumeButton(hass, door))
        entities.append(DoorOverrideUntilNextScheduleButton(hass, door))
        entities.append(DoorTimedOverrideUnlockButton(hass, door))
        entities.append(DoorOverrideUntilResumeCardOrPinButton(hass, door))

    async_add_entities(entities)


class BaseDoorButton(ProtectorNetDevice, ButtonEntity):
    def __init__(self, hass: HomeAssistant, door: dict):
        self.hass = hass
        self.door_id = door["Id"]
        self.door_name = door.get("Name", "Unknown Door")


class DoorPulseUnlockButton(BaseDoorButton):
    def __init__(self, hass: HomeAssistant, door: dict):
        super().__init__(hass, door)
        self._attr_name = f"{self.door_name} Pulse Unlock"
        self._attr_unique_id = f"protector_net_{self.door_id}_pulse"

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
        self._attr_unique_id = f"protector_net_{self.door_id}_until_resume"

    async def async_press(self):
        await api.set_override(self.hass, [self.door_id], "Resume")


class DoorOverrideUntilNextScheduleButton(BaseDoorButton):
    def __init__(self, hass: HomeAssistant, door: dict):
        super().__init__(hass, door)
        self._attr_name = f"{self.door_name} Unlock Until Next Schedule"
        self._attr_unique_id = f"protector_net_{self.door_id}_until_next_schedule"

    async def async_press(self):
        await api.set_override(self.hass, [self.door_id], "Schedule")

class DoorOverrideUntilResumeCardOrPinButton(BaseDoorButton):
    def __init__(self, hass: HomeAssistant, door: dict):
        super().__init__(hass, door)
        self._attr_name = f"{self.door_name} CardOrPin Until Resume"
        self._attr_unique_id = f"protector_net_{self.door_id}_until_resume_card_or_pin"

    async def async_press(self):
        await api.override_until_resume_card_or_pin(self.hass, [self.door_id])


class DoorTimedOverrideUnlockButton(BaseDoorButton):
    def __init__(self, hass: HomeAssistant, door: dict):
        super().__init__(hass, door)
        self._attr_name = f"{self.door_name} Timed Override Unlock"
        self._attr_unique_id = f"protector_net_{self.door_id}_timed_override"

    async def async_press(self):
        minutes = self.hass.data[DOMAIN].get("default_override_minutes", 5)
        await api.set_override(self.hass, [self.door_id], "Time", minutes=minutes)

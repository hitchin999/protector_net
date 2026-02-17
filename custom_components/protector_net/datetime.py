"""Override Until datetime entity for Protector.Net doors.

Provides a date+time picker per door so users can select *when* the override
should expire instead of calculating minutes manually.  When the Override
switch is turned ON with type "For Specified Time", the switch checks this
entity first and auto-computes minutes.  If this entity is empty or in the
past, the switch falls back to the Override Minutes number entity.
"""

from __future__ import annotations

import logging
from datetime import datetime as dt_datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, UI_STATE
from .device import ProtectorNetDevice
from . import api

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create an Override Until datetime entity per door."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(entry.entry_id, {})
    hass.data[DOMAIN][entry.entry_id].setdefault(UI_STATE, {})

    try:
        doors = await api.get_all_doors(hass, entry.entry_id)
    except Exception as e:
        _LOGGER.error("[%s] Failed to fetch doors for datetime entities: %s", entry.entry_id, e)
        doors = []

    entities: List[DateTimeEntity] = []
    for door in doors or []:
        entities.append(OverrideUntilDatetime(hass, entry, door))

    if entities:
        async_add_entities(entities)
        _LOGGER.debug("[%s] Added %d datetime entities", entry.entry_id, len(entities))
    else:
        _LOGGER.debug("[%s] No datetime entities to add", entry.entry_id)


class OverrideUntilDatetime(ProtectorNetDevice, DateTimeEntity):
    """Per-door 'Override Until' datetime picker.

    Stores the target end-time for a timed override.  The Override switch
    reads this value (via the shared UI bucket) and computes minutes
    automatically when turning on.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, door: dict) -> None:
        super().__init__(entry)
        self.hass = hass
        self._entry = entry
        self._entry_id = entry.entry_id
        self._door = door
        self.door_id: int = int(door["Id"])
        self.door_name: str = door.get("Name", f"Door {self.door_id}")

        entry_data = hass.data[DOMAIN][self._entry_id]
        self._host_key: str = entry_data.get("host") or (urlparse(entry.data["base_url"]).netloc or "")
        self._hub_identifier: str = entry_data.get("hub_identifier", f"hub:{self._host_key}|{self._entry_id}")

        # Shared UI bucket (same dict the switch/select/number entities use)
        self._ui: Dict[str, Any] = hass.data[DOMAIN][self._entry_id][UI_STATE].setdefault(
            self.door_id, {},
        )

        host_safe = (urlparse(entry.data["base_url"]).hostname or "").replace(":", "_")
        self._attr_name = "Override Until"
        self._attr_unique_id = f"protector_net_{host_safe}_{self._entry_id}_{self.door_id}_override_until"

        # Initialize from UI bucket (may be None)
        self._attr_native_value: dt_datetime | None = self._ui.get("override_until")

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"door:{self._host_key}:{self.door_id}|{self._entry_id}")},
            "name": self.door_name,
            "manufacturer": "Yoel Goldstein/Vaayer LLC",
            "model": "Protector.Net Door",
            "via_device": (DOMAIN, self._hub_identifier),
            "configuration_url": self._entry.data.get("base_url"),
        }

    async def async_set_value(self, value: dt_datetime) -> None:
        """Handle user picking a new date+time."""
        # Ensure timezone-aware (HA's datetime picker may provide aware or naive)
        if value.tzinfo is None:
            value = dt_util.as_local(value)

        self._attr_native_value = value
        self._ui["override_until"] = value
        self.async_write_ha_state()
        _LOGGER.debug(
            "[%s] Door %s: Override Until set to %s",
            self._entry_id, self.door_id, value.isoformat(),
        )

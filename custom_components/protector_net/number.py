from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    UI_STATE,
    DEFAULT_OVERRIDE_MINUTES,
    OVERRIDE_TYPE_OPTIONS,
    OVERRIDE_MODE_OPTIONS,
    OVERRIDE_TYPE_LABEL_TO_TOKEN,
    OVERRIDE_MODE_LABEL_TO_TOKEN,
)
from .device import ProtectorNetDevice
from .discovery import async_setup_door_platform_backfill
from . import api

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a minutes Number entity per door."""
    # Ensure shared UI bucket exists
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(entry.entry_id, {})
    hass.data[DOMAIN][entry.entry_id].setdefault(UI_STATE, {})

    added_door_ids: set[int] = set()

    @callback
    def _add_doors(door_list: List[dict]) -> None:
        entities: List[NumberEntity] = [
            OverrideMinutesNumber(hass, entry, d) for d in door_list
        ]
        added_door_ids.update(int(d["Id"]) for d in door_list if "Id" in d)
        if entities:
            async_add_entities(entities)
            _LOGGER.debug("[%s] Added %d number entities", entry.entry_id, len(entities))

    try:
        doors = await api.get_all_doors(hass, entry.entry_id)
    except Exception as e:
        _LOGGER.error("[%s] Failed to fetch doors for numbers: %s", entry.entry_id, e)
        doors = []

    _add_doors([d for d in (doors or []) if "Id" in d])

    # Self-heal: re-add these once the WS reconnects if Hartmann was
    # unreachable above, instead of leaving them "unavailable".
    async_setup_door_platform_backfill(
        hass, entry,
        added_door_ids=added_door_ids,
        add_doors=_add_doors,
        label="number",
    )


class OverrideMinutesNumber(ProtectorNetDevice, NumberEntity):
    """Per-door minutes field used when Override Type = For Specified Time."""

    _attr_has_entity_name = True
    _attr_native_min_value = 1
    _attr_native_max_value = 2147483647  # int32 max - Hartmann accepts very large values
    _attr_native_step = 1
    _attr_unit_of_measurement = "min"
    _attr_mode = "box"  # direct entry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, door: dict):
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

        # Per-door shared state bucket
        ui_bucket = hass.data[DOMAIN][self._entry_id].setdefault(UI_STATE, {})
        self._ui: Dict[str, Any] = ui_bucket.setdefault(
            self.door_id,
            {
                "type": OVERRIDE_TYPE_OPTIONS[0],
                "mode": OVERRIDE_MODE_OPTIONS[0],
                "minutes": entry.options.get("override_minutes", entry.data.get("override_minutes", DEFAULT_OVERRIDE_MINUTES)),
                "active": False,
            },
        )

        host_safe = (urlparse(entry.data["base_url"]).hostname or "").replace(":", "_")
        self._attr_name = "Override Minutes"
        self._attr_unique_id = f"protector_net_{host_safe}_{self._entry_id}_{self.door_id}_override_minutes"

        # Initialize UI value
        self._attr_native_value = int(self._ui.get("minutes", DEFAULT_OVERRIDE_MINUTES))

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

    async def async_set_native_value(self, value: float) -> None:
        minutes = max(int(value), 1)
        self._ui["minutes"] = minutes
        self._attr_native_value = minutes
        self.async_write_ha_state()

        # If the switch is ON and the type is Time, re-apply immediately
        if self._ui.get("active") and self._ui.get("type", "").lower().startswith("for specified time"):
            await _apply_override_from_ui(self.hass, self._entry_id, self.door_id, self._ui)


# --- local helper (avoid circular import with select.py) ---------------------

async def _apply_override_from_ui(hass: HomeAssistant, entry_id: str, door_id: int, ui: Dict[str, Any]) -> None:
    """Build and send an override using the current UI state dict."""
    type_label: str = ui.get("type") or OVERRIDE_TYPE_OPTIONS[0]
    mode_label: str = ui.get("mode") or OVERRIDE_MODE_OPTIONS[0]
    minutes: int = int(ui.get("minutes", DEFAULT_OVERRIDE_MINUTES))

    type_token = OVERRIDE_TYPE_LABEL_TO_TOKEN.get(type_label.lower())
    mode_token = OVERRIDE_MODE_LABEL_TO_TOKEN.get(mode_label.lower())

    if not type_token or not mode_token:
        _LOGGER.error("[%s] Invalid override UI -> type=%s, mode=%s", entry_id, type_label, mode_label)
        return

    ok = await api.apply_override(
        hass,
        entry_id,
        [door_id],
        override_type=type_token,
        mode=mode_token,
        minutes=(minutes if type_token == "Time" else None),
    )
    if not ok:
        _LOGGER.error("[%s] Failed to apply override to door %s", entry_id, door_id)

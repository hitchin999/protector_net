from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    DOMAIN,
    UI_STATE,
    OVERRIDE_TYPE_OPTIONS,
    OVERRIDE_MODE_OPTIONS,      # includes "None"
    DEFAULT_OVERRIDE_TYPE,
    DEFAULT_OVERRIDE_MODE,
    DEFAULT_OVERRIDE_MINUTES,
    TZ_INDEX_TO_FRIENDLY,
)
from .device import ProtectorNetDevice
from . import api

_LOGGER = logging.getLogger(__name__)

DISPATCH_DOOR = f"{DOMAIN}_door_event"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(entry.entry_id, {})
    hass.data[DOMAIN][entry.entry_id].setdefault(UI_STATE, {})

    try:
        doors = await asyncio.wait_for(api.get_all_doors(hass, entry.entry_id), timeout=30)
    except Exception as e:
        _LOGGER.error("[%s] Failed to fetch doors for selects: %s", entry.entry_id, e)
        doors = []

    entities: List[SelectEntity] = []
    for door in doors or []:
        entities.append(OverrideTypeSelect(hass, entry, door))
        entities.append(OverrideModeSelect(hass, entry, door))

    if entities:
        async_add_entities(entities, update_before_add=True)
        _LOGGER.debug("[%s] Added %d select entities", entry.entry_id, len(entities))


class _DoorEntityBase(ProtectorNetDevice):
    _attr_should_poll = False  # we push updates

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
        self._host_full: str = (entry.data["base_url"].split("://", 1)[1]) if "base_url" in entry.data else self._host_key

        self._ui = self.hass.data[DOMAIN][self._entry_id][UI_STATE].setdefault(
            self.door_id,
            {
                "type": DEFAULT_OVERRIDE_TYPE,
                "mode_selected": DEFAULT_OVERRIDE_MODE,  # desired mode for next ON
                "reader_mode": None,                     # last reader mode label seen
                "active": False,                         # true while overridden
                "minutes": int(entry.options.get("override_minutes", entry.data.get("override_minutes", DEFAULT_OVERRIDE_MINUTES))),
            },
        )

        self._unsub_dispatch: Optional[callable] = None
        self._unsub_sensor_listeners: list[callable] = []
        self._overridden_entity_id: Optional[str] = None
        self._reader_mode_entity_id: Optional[str] = None
        self._sensor_bind_attempts: int = 0

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"door:{self._host_key}:{self.door_id}|{self._entry_id}")},
            "name": self.door_name,
            "manufacturer": "Hartmann Controls",
            "model": "Protector.Net Door",
            "via_device": (DOMAIN, self._hub_identifier),
            "configuration_url": self._entry.data.get("base_url"),
        }

    async def async_added_to_hass(self) -> None:
        # WS push (door status)
        self._unsub_dispatch = async_dispatcher_connect(
            self.hass,
            f"{DISPATCH_DOOR}_{self._entry_id}",
            self._handle_door_status,
        )
        # Bind to sibling sensors (for instant mirroring)
        self._bind_sensor_watchers()
        # Seed from current sensor states now
        self._seed_from_sensors_and_push()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_dispatch:
            self._unsub_dispatch()
            self._unsub_dispatch = None
        for u in self._unsub_sensor_listeners:
            try:
                u()
            except Exception:
                pass
        self._unsub_sensor_listeners.clear()

    # ---------- Sensor binding / seeding ----------

    def _resolve_sensor_entity_ids(self) -> None:
        reg = er.async_get(self.hass)
        uid_over = f"{DOMAIN}_{self._host_full}_door_{self.door_id}_overridden|{self._entry_id}"
        uid_reader = f"{DOMAIN}_{self._host_full}_door_{self.door_id}_reader_mode|{self._entry_id}"
        ent_over = next((e for e in reg.entities.values() if e.unique_id == uid_over), None)
        ent_reader = next((e for e in reg.entities.values() if e.unique_id == uid_reader), None)
        self._overridden_entity_id = ent_over.entity_id if ent_over else None
        self._reader_mode_entity_id = ent_reader.entity_id if ent_reader else None

    def _bind_sensor_watchers(self) -> None:
        """Subscribe to 'Overridden' and 'Reader Mode' sensors for instant updates."""
        self._resolve_sensor_entity_ids()

        for u in self._unsub_sensor_listeners:
            try:
                u()
            except Exception:
                pass
        self._unsub_sensor_listeners.clear()

        ids: list[str] = []
        if self._overridden_entity_id:
            ids.append(self._overridden_entity_id)
        if self._reader_mode_entity_id:
            ids.append(self._reader_mode_entity_id)

        if not ids:
            # Sensors may register slightly later; keep retrying a few times.
            if self._sensor_bind_attempts < 30:
                self._sensor_bind_attempts += 1
                self.hass.loop.call_later(1.0, self._bind_sensor_watchers)
            return

        @callback
        def _on_dep_state_change(event) -> None:
            self._seed_from_sensors_and_push()

        self._unsub_sensor_listeners.append(
            async_track_state_change_event(self.hass, ids, _on_dep_state_change)
        )

    def _seed_from_sensors_and_push(self) -> None:
        """Read Overridden + Reader Mode sensors and push the select value immediately."""
        # Overridden
        if self._overridden_entity_id:
            st = self.hass.states.get(self._overridden_entity_id)
            if st and st.state in ("On", "Off"):
                self._ui["active"] = (st.state == "On")

        # Reader mode
        if self._reader_mode_entity_id:
            st = self.hass.states.get(self._reader_mode_entity_id)
            if st and st.state:
                self._ui["reader_mode"] = st.state

        self._after_ws_bucket_update()

    # ---------- Live WS path ----------

    @callback
    def _handle_door_status(self, data: Dict[str, Any]) -> None:
        if int(data.get("door_id", -1)) != self.door_id:
            return
        st = data.get("status") or {}

        # Update bucket from WS
        if "overridden" in st:
            self._ui["active"] = bool(st["overridden"])
            if not self._ui["active"]:
                self._ui["mode_selected"] = "None"

        if "timeZone" in st:
            tz_idx = st.get("timeZone")
            try:
                tz_idx = int(tz_idx)
            except (TypeError, ValueError):
                tz_idx = None
            if tz_idx is not None:
                friendly = TZ_INDEX_TO_FRIENDLY.get(tz_idx)
                if friendly:
                    self._ui["reader_mode"] = friendly

        # Always recompute/push on any frame
        self._after_ws_bucket_update()

    # child classes should compute and push their visible state here
    def _after_ws_bucket_update(self) -> None:
        return


# ───────────────────────── Type select ─────────────────────────

class OverrideTypeSelect(_DoorEntityBase, SelectEntity):
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, door: dict):
        super().__init__(hass, entry, door)
        self._attr_name = "Override Type"
        host_safe = (urlparse(entry.data["base_url"]).hostname or "").replace(":", "_")
        self._attr_unique_id = f"protector_net_{host_safe}_{self._entry_id}_{self.door_id}_override_type"
        self._attr_options = list(OVERRIDE_TYPE_OPTIONS)
        self._attr_current_option = self._ui.get("type", DEFAULT_OVERRIDE_TYPE)

    @property
    def device_class(self) -> Optional[str]:
        return None

    def _after_ws_bucket_update(self) -> None:
        cur = self._ui.get("type", DEFAULT_OVERRIDE_TYPE)
        if self._attr_current_option != cur:
            self._attr_current_option = cur
            self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        option = option.strip()
        if option not in OVERRIDE_TYPE_OPTIONS:
            raise ValueError(f"Invalid override type: {option}")
        self._ui["type"] = option
        self._after_ws_bucket_update()


# ───────────────────────── Mode select ─────────────────────────

class OverrideModeSelect(_DoorEntityBase, SelectEntity):
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, door: dict):
        super().__init__(hass, entry, door)
        self._attr_name = "Override Mode"
        host_safe = (urlparse(entry.data["base_url"]).hostname or "").replace(":", "_")
        self._attr_unique_id = f"protector_net_{host_safe}_{self._entry_id}_{self.door_id}_override_mode"
        self._attr_options = list(OVERRIDE_MODE_OPTIONS)
        self._attr_current_option = "None"

    @property
    def device_class(self) -> Optional[str]:
        return None

    # --- label normalizer: map reader label -> one of our exact options ---
    def _match_option(self, label: Optional[str]) -> Optional[str]:
        if not label:
            return None
        # exact match first
        for opt in self._attr_options:
            if opt == label:
                return opt
        # try case-insensitive + whitespace-insensitive
        norm = label.lower().replace(" ", "")
        alts = {
            norm,
            norm.replace("and", "and"),
            norm.replace("or", "or"),
        }
        for opt in self._attr_options:
            o_norm = opt.lower().replace(" ", "")
            if o_norm in alts or o_norm == norm:
                return opt
        # try converting "Card or Pin" -> "CardOrPin", "Card and Pin" -> "CardAndPin"
        squashed = label.replace(" and ", "And ").replace(" or ", "Or ").replace(" ", "")
        for opt in self._attr_options:
            if opt.lower().replace(" ", "") == squashed.lower():
                return opt
        return None

    def _desired_option(self) -> str:
        # OFF -> "None"
        if not self._ui.get("active"):
            return "None"
        # ON -> mirror reader mode, but normalize to one of our options
        rm = self._ui.get("reader_mode")
        mapped = self._match_option(rm)
        return mapped or "None"

    def _after_ws_bucket_update(self) -> None:
        desired = self._desired_option()
        if self._attr_current_option != desired:
            _LOGGER.debug(
                "[%s] Door %s OverrideModeSelect -> %s (active=%s, reader=%r, options=%r)",
                self._entry_id, self.door_id, desired, self._ui.get("active"), self._ui.get("reader_mode"), self._attr_options
            )
            self._attr_current_option = desired
            self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        option = option.strip()
        if option not in self._attr_options:
            raise ValueError(f"Invalid override mode: {option}")

        # This sets the *desired* mode for next ON. Display still mirrors reader_mode when ON.
        self._ui["mode_selected"] = option if option != "None" else "None"
        # Recompute display (will be None if OFF, or mirror reader if ON)
        self._after_ws_bucket_update()

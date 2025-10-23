from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers import entity_registry as er

from .device import ProtectorNetDevice
from . import api
from .const import (
    DOMAIN,
    UI_STATE,
    DEFAULT_OVERRIDE_TYPE,
    DEFAULT_OVERRIDE_MODE,
    DEFAULT_OVERRIDE_MINUTES,
    OVERRIDE_TYPE_LABEL_TO_TOKEN,
    OVERRIDE_MODE_LABEL_TO_TOKEN,
    TZ_INDEX_TO_FRIENDLY,
    FRIENDLY_TO_TZ_INDEX,   # used to compute a tz index for local status echo
)

_LOGGER = logging.getLogger(__name__)

# WS channel used by sensors/selects
DISPATCH_DOOR = f"{DOMAIN}_door_event"
DISPATCH_HUB = f"{DOMAIN}_hub_event"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create per-door Override switches + one 'All Doors – Lockdown Mode' switch per entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(entry.entry_id, {})
    hass.data[DOMAIN][entry.entry_id].setdefault(UI_STATE, {})

    # ----- Per-door override switches -----
    try:
        await asyncio.sleep(0.4)
        doors = await api.get_all_doors(hass, entry.entry_id)
    except Exception as e:
        _LOGGER.error("[%s] Failed to fetch doors for override switches: %s", entry.entry_id, e)
        doors = []

    entities: list[SwitchEntity] = [OverrideSwitch(hass, entry, d) for d in (doors or [])]

    # ----- One "All Doors – Lockdown Mode" switch per entry -----
    if doors:
        try:
            entities.append(AllDoorsLockdownSwitch(hass, entry, doors))
        except Exception as e:
            _LOGGER.error("[%s] Failed to create All Doors Lockdown switch: %s", entry.entry_id, e)

    if entities:
        async_add_entities(entities)
        _LOGGER.debug("[%s] Added %d switches (incl. All Doors Lockdown)", entry.entry_id, len(entities))
    else:
        _LOGGER.debug("[%s] No switches to add", entry.entry_id)


# =====================================================================
# Per-door Override switch  (your existing one, kept with tiny tweaks)
# =====================================================================

class OverrideSwitch(ProtectorNetDevice, SwitchEntity):
    """Master 'Override' control for a door."""

    _attr_has_entity_name = True
    _attr_name = "Override"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, door: Dict[str, Any]) -> None:
        super().__init__(entry)
        self.hass = hass
        self._entry = entry
        self._entry_id = entry.entry_id
        self._door = door

        self._door_id: int = int(door["Id"])
        self._door_name: str = door.get("Name") or f"Door {self._door_id}"

        entry_data = hass.data[DOMAIN][self._entry_id]
        self._host_key: str = entry_data.get("host") or (urlparse(entry.data["base_url"]).netloc or "")
        self._hub_identifier: str = entry_data.get("hub_identifier", f"hub:{self._host_key}|{self._entry_id}")

        host_safe = (urlparse(entry.data["base_url"]).hostname or "").replace(":", "_")
        self._attr_unique_id = f"protector_net_{host_safe}_{self._entry_id}_{self._door_id}_override_switch"

        self._ui = self.hass.data[DOMAIN][self._entry_id][UI_STATE].setdefault(
            self._door_id,
            {
                "type": DEFAULT_OVERRIDE_TYPE,
                "mode_selected": DEFAULT_OVERRIDE_MODE,  # desired mode to apply when turning ON
                "reader_mode": None,                     # last reader mode seen (friendly)
                "minutes": int(entry.options.get("override_minutes", entry.data.get("override_minutes", DEFAULT_OVERRIDE_MINUTES))),
                "active": False,
            },
        )

        self._is_on: bool = bool(self._ui.get("active"))
        self._busy: bool = False
        self._unsub_dispatch = None

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"door:{self._host_key}:{self._door_id}|{self._entry_id}")},
            "name": self._door_name,
            "manufacturer": "Yoel Goldstein/Vaayer LLC",
            "model": "Protector.Net Door",
            "via_device": (DOMAIN, self._hub_identifier),
            "configuration_url": self._entry.data.get("base_url"),
        }

    async def async_added_to_hass(self) -> None:
        self._unsub_dispatch = async_dispatcher_connect(
            self.hass, f"{DISPATCH_DOOR}_{self._entry_id}", self._on_door_status
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_dispatch:
            self._unsub_dispatch()
            self._unsub_dispatch = None

    # ----------------------------
    # User interactions
    # ----------------------------
    async def async_turn_on(self, **kwargs) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            mode_label: str = str(self._ui.get("mode_selected", DEFAULT_OVERRIDE_MODE))
            if mode_label == "None":
                _LOGGER.warning(
                    "[%s] Door %s: Override ON requested while Mode=None; ignoring.",
                    self._entry_id, self._door_id
                )
                self._set_local_active(False)
                return

            type_label: str = str(self._ui.get("type", DEFAULT_OVERRIDE_TYPE))
            minutes: int = int(self._ui.get("minutes", DEFAULT_OVERRIDE_MINUTES))

            type_token = OVERRIDE_TYPE_LABEL_TO_TOKEN.get(type_label.lower())
            mode_token = OVERRIDE_MODE_LABEL_TO_TOKEN.get(mode_label.lower())
            if not type_token or not mode_token:
                _LOGGER.error("[%s] Door %s: Invalid type/mode -> %r / %r", self._entry_id, self._door_id, type_label, mode_label)
                self._set_local_active(False)
                return

            minutes_arg = minutes if type_token == "Time" else None

            ok = await api.apply_override(
                self.hass,
                self._entry_id,
                [self._door_id],
                override_type=type_token,
                mode=mode_token,
                minutes=minutes_arg,
            )
            if not ok:
                _LOGGER.error("[%s] Door %s: apply_override failed", self._entry_id, self._door_id)
                self._set_local_active(False)
                return

            # Optimistic ON + echo a local door-status so selects/sensors update instantly
            self._set_local_active(True)
            tz_idx = FRIENDLY_TO_TZ_INDEX.get(mode_label)
            async_dispatcher_send(
                self.hass,
                f"{DISPATCH_DOOR}_{self._entry_id}",
                {"door_id": self._door_id, "status": {"overridden": True, **({"timeZone": tz_idx} if tz_idx is not None else {})}},
            )

        finally:
            self._busy = False

    async def async_turn_off(self, **kwargs) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            ok = await api.resume_schedule(self.hass, self._entry_id, [self._door_id])
            if not ok:
                _LOGGER.error("[%s] Door %s: resume_schedule failed", self._entry_id, self._door_id)
                return

            # Optimistic OFF + local door-status (so Override Mode select flips to "None" immediately)
            self._set_local_active(False)
            self._ui["mode_selected"] = "None"
            async_dispatcher_send(
                self.hass,
                f"{DISPATCH_DOOR}_{self._entry_id}",
                {"door_id": self._door_id, "status": {"overridden": False}},
            )

        finally:
            self._busy = False

    # ----------------------------
    # WS handling
    # ----------------------------
    @callback
    def _on_door_status(self, payload: Dict[str, Any]) -> None:
        if int(payload.get("door_id", -1)) != self._door_id:
            return

        st = payload.get("status") or {}
        changed = False

        if "overridden" in st:
            new_active = bool(st["overridden"])
            if self._ui.get("active") != new_active:
                self._ui["active"] = new_active
            if self._is_on != new_active:
                self._is_on = new_active
                changed = True
            if not new_active and self._ui.get("mode_selected") != "None":
                self._ui["mode_selected"] = "None"
                changed = True

        tz_idx = st.get("timeZone", None)
        if tz_idx is not None:
            try:
                tz_idx = int(tz_idx)
            except (TypeError, ValueError):
                tz_idx = None
            if tz_idx is not None:
                friendly = TZ_INDEX_TO_FRIENDLY.get(tz_idx)
                if friendly and self._ui.get("reader_mode") != friendly:
                    self._ui["reader_mode"] = friendly
                    changed = True

        if changed:
            self.async_write_ha_state()

    # ----------------------------
    # Helpers
    # ----------------------------
    def _set_local_active(self, active: bool) -> None:
        if self._ui.get("active") != active:
            self._ui["active"] = active
        if self._is_on != active:
            self._is_on = active
            self.async_write_ha_state()


# =====================================================================
# All Doors – Lockdown Mode switch  (new)
# =====================================================================

class AllDoorsLockdownSwitch(ProtectorNetDevice, SwitchEntity):
    """
    Switch that applies 'Lockdown (Until Resume)' to ALL doors in this entry.
    - ON  => apply_override(mode=Lockdown, type=Until Resume) to every door
    - OFF => resume_schedule for every door
    State rules:
      - is_on == True when EVERY mapped door is in Lockdown reader mode (by WS/sensors)
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Lockdown Mode"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, doors: List[Dict[str, Any]]) -> None:
        super().__init__(entry)
        self.hass = hass
        self._entry = entry
        self._entry_id = entry.entry_id

        entry_data = hass.data[DOMAIN][self._entry_id]
        base_url = entry.data.get("base_url")
        self._host_key: str = entry_data.get("host") or (urlparse(base_url).netloc if base_url else "")
        self._hub_identifier: str = entry_data.get("hub_identifier", f"hub:{self._host_key}|{self._entry_id}")
        host_safe = (urlparse(base_url).hostname or "").replace(":", "_") if base_url else self._host_key.replace(":", "_")

        # Partition name for device label (try stored, else derive from title)
        partition_name = (
            entry_data.get("partition_name")
            or (entry.title.split("–", 1)[1].strip() if "–" in (entry.title or "") else entry_data.get("partition_id", "All"))
        )

        # Device identification (hub-level)
        self._device_ident = f"alldoors:{self._host_key}|{self._entry_id}"
        self._attr_unique_id = f"protector_net_{host_safe}_{self._entry_id}_alldoors_lockdown_switch"
        self._door_ids: List[int] = [int(d["Id"]) for d in (doors or [])]

        # Track current per-door reader/override status for quick aggregation
        self._state_by_door: Dict[int, Dict[str, Any]] = {int(d["Id"]): {"active": None, "reader_mode": None} for d in (doors or [])}

        # Exposed device info
        self._device_info = {
            "identifiers": {(DOMAIN, self._device_ident)},
            "manufacturer": "Yoel Goldstein/Vaayer LLC",
            "model": "Protector.Net Partition",
            "name": f"All Doors – {partition_name}",
            "via_device": (DOMAIN, self._hub_identifier),
            "configuration_url": base_url,
        }

        self._is_on = False
        self._busy = False
        self._unsub_dispatch = None

        # Quick boot seeding from sibling sensors (optional but makes it snappy)
        self._host_full: str = base_url.split("://", 1)[1] if base_url else self._host_key

        self._seed_from_sensors_once()

    # ------------- HA properties -------------

    @property
    def device_info(self):
        return dict(self._device_info)

    @property
    def is_on(self) -> bool:
        return self._is_on

    # ------------- Lifecycle -------------

    async def async_added_to_hass(self) -> None:
        self._unsub_dispatch = async_dispatcher_connect(
            self.hass, f"{DISPATCH_DOOR}_{self._entry_id}", self._on_door_status
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_dispatch:
            self._unsub_dispatch()
            self._unsub_dispatch = None

    # ------------- User actions -------------

    async def async_turn_on(self, **kwargs) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            # Tokens
            type_token = OVERRIDE_TYPE_LABEL_TO_TOKEN.get("until resume") or OVERRIDE_TYPE_LABEL_TO_TOKEN.get("resume") or "Resume"
            mode_token = OVERRIDE_MODE_LABEL_TO_TOKEN.get("lockdown") or "Lockdown"

            ok = await api.apply_override(
                self.hass,
                self._entry_id,
                self._door_ids,
                override_type=type_token,
                mode=mode_token,
                minutes=None,
            )
            if not ok:
                _LOGGER.error("[%s] AllDoors: apply_override Lockdown failed", self._entry_id)
                return

            # Optimistic echo for all doors
            tz_idx = FRIENDLY_TO_TZ_INDEX.get("Lockdown")
            for did in self._door_ids:
                async_dispatcher_send(
                    self.hass,
                    f"{DISPATCH_DOOR}_{self._entry_id}",
                    {"door_id": did, "status": {"overridden": True, **({"timeZone": tz_idx} if tz_idx is not None else {})}},
                )

            self._recompute_and_push()

        finally:
            self._busy = False

    async def async_turn_off(self, **kwargs) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            ok = await api.resume_schedule(self.hass, self._entry_id, self._door_ids)
            if not ok:
                _LOGGER.error("[%s] AllDoors: resume_schedule failed", self._entry_id)
                return

            # Optimistic echo for all doors
            for did in self._door_ids:
                async_dispatcher_send(
                    self.hass,
                    f"{DISPATCH_DOOR}_{self._entry_id}",
                    {"door_id": did, "status": {"overridden": False}},
                )

            self._recompute_and_push()

        finally:
            self._busy = False

    # ------------- WS handling + state aggregation -------------

    @callback
    def _on_door_status(self, payload: Dict[str, Any]) -> None:
        did = payload.get("door_id")
        if did is None or int(did) not in self._door_ids:
            return
        st = payload.get("status") or {}

        rec = self._state_by_door.setdefault(int(did), {"active": None, "reader_mode": None})

        if "overridden" in st:
            rec["active"] = bool(st["overridden"])

        if "timeZone" in st:
            tz_idx = st.get("timeZone")
            try:
                tz_idx = int(tz_idx)
            except (TypeError, ValueError):
                tz_idx = None
            if tz_idx is not None:
                friendly = TZ_INDEX_TO_FRIENDLY.get(tz_idx)
                if friendly:
                    rec["reader_mode"] = friendly

        self._recompute_and_push()

    def _recompute_and_push(self) -> None:
        """is_on if EVERY known door is in reader_mode 'Lockdown' (best-effort)."""
        if not self._door_ids:
            new_state = False
        else:
            all_seen = True
            all_lockdown = True
            for did in self._door_ids:
                rec = self._state_by_door.get(did) or {}
                # We consider a door "lockdown" when its reader mode says Lockdown.
                rm = (rec.get("reader_mode") or "").strip()
                if not rm:
                    all_seen = False
                    all_lockdown = False
                    break
                if rm.lower() != "lockdown":
                    all_lockdown = False
            new_state = all_lockdown and all_seen

        if self._is_on != new_state:
            self._is_on = new_state
            self.async_write_ha_state()

    # ------------- Boot seed (optional but helpful) -------------

    def _seed_from_sensors_once(self) -> None:
        """Use existing Overridden/Reader Mode sensors at boot to initialize aggregation."""
        reg = er.async_get(self.hass)
        for did in self._door_ids:
            uid_over = f"{DOMAIN}_{self._host_full}_door_{did}_overridden|{self._entry_id}"
            uid_reader = f"{DOMAIN}_{self._host_full}_door_{did}_reader_mode|{self._entry_id}"
            ent_over = next((e for e in reg.entities.values() if e.unique_id == uid_over), None)
            ent_reader = next((e for e in reg.entities.values() if e.unique_id == uid_reader), None)
            if ent_over:
                st = self.hass.states.get(ent_over.entity_id)
                if st and st.state in ("On", "Off"):
                    self._state_by_door[did]["active"] = (st.state == "On")
            if ent_reader:
                st = self.hass.states.get(ent_reader.entity_id)
                if st and st.state:
                    self._state_by_door[did]["reader_mode"] = st.state
        self._recompute_and_push()

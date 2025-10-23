from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription, SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNKNOWN, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(f"{DOMAIN}.sensor")

# Dispatcher channels (must match ws.py)
DISPATCH_DOOR = f"{DOMAIN}_door_event"
DISPATCH_HUB = f"{DOMAIN}_hub_event"
DISPATCH_LOG = f"{DOMAIN}_door_log"  # Last Door Log updates

# Reader-mode mapping (full)
MODE_MAP = {
    0: "Lockdown",
    1: "Card",
    2: "Pin",
    3: "Card or Pin",
    4: "Card and Pin",
    5: "Unlock",     # note: "Unlock" (not "Unlocked")
    6: "First Credential In",
    7: "Dual Credential",
    8: "Lockdown",          # keep for compatibility
}

# Enum options for the three sensors
LOCK_STATE_OPTIONS = ["Locked", "Unlocked"]   # note: "Unlocked" (not "Unlock")
OVERRIDDEN_OPTIONS = ["On", "Off"]

# Reader mode options shown in the UI (match your MODE_MAP wording)
READER_MODE_OPTIONS = [
    "Lockdown",
    "Card",
    "Pin",
    "Card or Pin",
    "Card and Pin",
    "Unlock",    # note: "Unlock" (not "Unlocked")
    "First Credential In",
    "Dual Credential",
]

# ---------------------------
# Helpers: parse & filter doors
# ---------------------------
def _iter_doors_from_overview(
    overview: dict[str, Any],
    *,
    site_name_contains: Optional[str] = None,
    status_roots: Optional[List[str]] = None,
) -> List[Tuple[int, str, str, str]]:
    """
    Return list of (door_id, door_name, status_id_key, site_name) from System Overview tree,
    filtered by:
      - site_name_contains: door must be under a top-level Site whose Name contains this text
      - status_roots: door StatusId must start with one of these roots (controller ids)
    """
    out: List[Tuple[int, str, str, str]] = []

    site_match = (site_name_contains or "").strip().lower() or None
    roots: Optional[List[str]] = None
    if status_roots:
        roots = [r.strip() for r in status_roots if r and r.strip()]
        if roots:
            roots = [r.split("::", 1)[0] for r in roots]

    def door_allowed(door_status_id: str, site_name: Optional[str]) -> bool:
        if site_match:
            if not site_name or site_match not in site_name.lower():
                return False
        if roots:
            root = door_status_id.split("::", 1)[0]
            if root not in roots:
                return False
        return True

    def walk(node: Dict[str, Any], current_site_name: Optional[str]) -> None:
        for sub in node.get("Nodes", []) or []:
            ntype = sub.get("Type")
            if ntype == "Site":
                walk(sub, sub.get("Name") or current_site_name)
            elif ntype == "Door":
                did = sub.get("Id")
                name = sub.get("Name")
                sid = sub.get("StatusId")
                if isinstance(did, int) and sid and name:
                    if door_allowed(str(sid), current_site_name):
                        out.append((did, str(name), str(sid), current_site_name or ""))
            else:
                walk(sub, current_site_name)

    root = (overview or {}).get("Status", {})
    for site in root.get("Nodes", []) or []:
        walk(site, site.get("Name"))

    return out


# ------------------------
# Entity descriptions
# ------------------------
@dataclass
class ProtectorDoorDesc(SensorEntityDescription):
    key: str
    device_class: Optional[str] = None


LOCK_STATE_DESC = ProtectorDoorDesc(
    key="lock_state",
    name="Lock State",
)

OVERRIDDEN_DESC = ProtectorDoorDesc(
    key="overridden",
    name="Overridden",
)

READER_MODE_DESC = ProtectorDoorDesc(
    key="reader_mode",
    name="Reader Mode",
)

LAST_LOG_DESC = ProtectorDoorDesc(
    key="last_log",
    name="Last Door Log by",
)


# ------------------------
# Setup
# ------------------------
async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Protector.Net sensors."""
    cfg = hass.data[DOMAIN][entry.entry_id]
    base_url: str = cfg["base_url"]
    host = base_url.split("://", 1)[1]

    # Create hub status sensor immediately so platform returns quickly
    hub_ent = ProtectorHubSensor(hass, entry.entry_id, base_url)
    async_add_entities([hub_ent])

    # Defer door discovery to a background task (don’t block startup)
    async def _add_doors_later() -> None:
        from . import api
        try:
            # Give the server a moment to warm after HA boot, and bound the call.
            await asyncio.sleep(0.5)
            overview = await asyncio.wait_for(api.get_system_overview(hass, entry.entry_id), timeout=30)
        except asyncio.TimeoutError:
            _LOGGER.error("[%s] System overview timed out; no door sensors will be created right now", entry.entry_id)
            return
        except Exception as e:
            _LOGGER.error("[%s] Failed to fetch system overview for sensors: %s", entry.entry_id, e)
            return

        # Pull optional filters
        opt = entry.options or {}
        site_name_contains: Optional[str] = opt.get("site_name_contains") or cfg.get("site_name_contains")
        raw_roots = opt.get("status_roots") or cfg.get("status_roots")

        if isinstance(raw_roots, str):
            status_roots = [s.strip() for s in raw_roots.split(",") if s.strip()]
        elif isinstance(raw_roots, list):
            status_roots = raw_roots
        else:
            status_roots = None

        if not site_name_contains:
            if "–" in (entry.title or ""):
                site_name_contains = (entry.title or "").split("–", 1)[1].strip()
            elif "-" in (entry.title or ""):
                site_name_contains = (entry.title or "").split("-", 1)[-1].strip()

        doors = _iter_doors_from_overview(
            overview,
            site_name_contains=site_name_contains,
            status_roots=status_roots,
        )
        _LOGGER.debug(
            "[%s] Doors after filter (site=%r, roots=%r): %d",
            entry.entry_id, site_name_contains, status_roots, len(doors)
        )

        if not doors:
            _LOGGER.warning("[%s] No doors matched filters; only Hub Status sensor will exist", entry.entry_id)
            return

        entities: List[SensorEntity] = []
        for did, dname, _status_id, _site_name in doors:
            entities.append(ProtectorDoorSensor(hass, entry.entry_id, base_url, did, dname, LOCK_STATE_DESC))
            entities.append(ProtectorDoorSensor(hass, entry.entry_id, base_url, did, dname, OVERRIDDEN_DESC))
            entities.append(ProtectorDoorSensor(hass, entry.entry_id, base_url, did, dname, READER_MODE_DESC))
            entities.append(ProtectorDoorLastLogSensor(hass, entry.entry_id, base_url, did, dname, LAST_LOG_DESC))

        async_add_entities(entities)
        _LOGGER.debug("[%s] Added %d door sensors", entry.entry_id, len(entities))

    hass.async_create_task(_add_doors_later())


# ------------------------
# Hub status sensor (minimal attrs) + RestoreEntity
# ------------------------
class ProtectorHubSensor(SensorEntity, RestoreEntity):
    """Shows connection/diagnostic info from ws client."""

    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry_id: str, base_url: str) -> None:
        self.hass = hass
        self._entry_id = entry_id
        self._base_url = base_url
        host = base_url.split("://", 1)[1]

        entry_data = hass.data[DOMAIN].get(entry_id, {})
        partition_name = (
            entry_data.get("partition_name")
            or (hass.config_entries.async_get_entry(entry_id).title.split("–", 1)[1].strip()
                if hass.config_entries.async_get_entry(entry_id)
                and "–" in hass.config_entries.async_get_entry(entry_id).title
                else str(entry_data.get("partition_id", "Unknown")))
        )

        # NEW: stash partition_id for attribute
        self._partition_id = entry_data.get("partition_id")

        self._attr_name = f"Hub Status – {partition_name}"
        self._attr_unique_id = f"{DOMAIN}_{host}_hub_status|{entry_id}"

        self._attr_native_value = "unknown"
        self._last_attrs: Dict[str, Any] = {}
        self._unsub: Optional[Callable[[], None]] = None
        self._partition_name = partition_name

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"hub:{self._base_url.split('://',1)[1]}|{self._entry_id}")},
            "manufacturer": "Yoel Goldstein/Vaayer LLC",
            "model": "Protector.Net Hub",
            "name": f"Hub Status – {self._partition_name}",
            "configuration_url": self._base_url,
        }

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        # Only expose the minimal set we want to see in normal operation
        return dict(self._last_attrs)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # Restore last state to avoid 'unknown' at boot
        last = await self.async_get_last_state()
        if last:
            if last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE, None, ""):
                self._attr_native_value = last.state
            # restore minimal attrs if present
            la = last.attributes or {}
            self._last_attrs = {
                "phase": la.get("phase"),
                "connected": la.get("connected"),
                "mapped_doors": la.get("mapped_doors"),
                "partition_id": la.get("partition_id", self._partition_id),
            }
            self.async_write_ha_state()

        signal = f"{DISPATCH_HUB}_{self._entry_id}"

        @callback
        def _hub_evt(data: dict[str, Any]) -> None:
            # Keep these **only**:
            self._last_attrs = {
                "phase": data.get("phase"),
                "connected": data.get("connected"),
                "mapped_doors": data.get("mapped_doors"),
                "partition_id": self._partition_id,  # <- minimal attribute set
            }
            self._attr_native_value = "running" if data.get("connected") else (data.get("phase") or "idle")
            self.async_write_ha_state()

        self._unsub = async_dispatcher_connect(self.hass, signal, _hub_evt)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None


# ------------------------
# Door sensors (3 metrics) + RestoreEntity
# ------------------------
class ProtectorDoorSensor(SensorEntity, RestoreEntity):
    """One door metric (Lock State / Overridden / Reader Mode) as ENUM sensors with fixed options."""

    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        base_url: str,
        door_id: int,
        door_name: str,
        desc: ProtectorDoorDesc,
    ) -> None:
        self.hass = hass
        self._entry_id = entry_id
        self._base_url = base_url
        self._door_id = int(door_id)
        self._door_name = door_name
        self.entity_description = desc
        host = base_url.split("://", 1)[1]

        label = desc.name or desc.key
        self._attr_name = f"{door_name} {label}"
        self._attr_unique_id = f"{DOMAIN}_{host}_door_{door_id}_{desc.key}|{entry_id}"

        # Make these ENUM sensors so the Automation UI shows dropdowns
        self._attr_device_class = SensorDeviceClass.ENUM
        if desc.key == "lock_state":
            self._attr_options = LOCK_STATE_OPTIONS
        elif desc.key == "overridden":
            self._attr_options = OVERRIDDEN_OPTIONS
        elif desc.key == "reader_mode":
            self._attr_options = READER_MODE_OPTIONS
        else:
            self._attr_options = None  # shouldn't happen

        self._attr_native_value: StateType = None
        self._unsub: Optional[Callable[[], None]] = None

    @property
    def device_info(self):
        host = self._base_url.split("://", 1)[1]
        return {
            "identifiers": {(DOMAIN, f"door:{host}:{self._door_id}|{self._entry_id}")},
            "manufacturer": "Hartmann Controls",
            "model": "Protector.Net Door",
            "name": self._door_name,
            "configuration_url": self._base_url,
            "via_device": (DOMAIN, f"hub:{host}|{self._entry_id}"),
        }

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        # Expose the enum choices so they’re visible in Templates/Developer Tools
        return {
            "Possible states": list(self._attr_options) if self._attr_options else None
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # Restore last state so we don't start as unknown
        last = await self.async_get_last_state()
        if last and last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE, None, ""):
            self._attr_native_value = last.state
            self.async_write_ha_state()

        signal = f"{DISPATCH_DOOR}_{self._entry_id}"

        @callback
        def _handle_door(evt: dict[str, Any]) -> None:
            if int(evt.get("door_id")) != self._door_id:
                return

            st = evt.get("status") or {}
            desc = self.entity_description
            new_value: Any = None
            update = False

            try:
                if desc.key == "lock_state":
                    # Show "Unlocked" or "Locked" (match options list)
                    strike = st.get("strike")
                    opener = st.get("opener")
                    if strike is not None or opener is not None:
                        if strike is True or opener is True:
                            new_value = "Unlocked"
                        elif strike is False and opener is False:
                            new_value = "Locked"
                        else:
                            new_value = None
                        update = new_value is not None

                elif desc.key == "overridden":
                    ov = st.get("overridden")
                    if ov is not None:
                        new_value = "On" if bool(ov) else "Off"
                        update = True

                elif desc.key == "reader_mode":
                    tz = st.get("timeZone")
                    if tz is not None:
                        try:
                            tz_int = int(tz)
                        except (TypeError, ValueError):
                            tz_int = tz
                        mapped = MODE_MAP.get(tz_int)
                        new_value = mapped if mapped is not None else str(tz_int)
                        update = True

            except Exception as e:
                _LOGGER.debug(
                    "[%s] value update failed for door %s (%s): %s",
                    self._entry_id,
                    self._door_id,
                    desc.key,
                    e,
                )

            if update and new_value != self._attr_native_value:
                self._attr_native_value = new_value
                self.async_write_ha_state()

        self._unsub = async_dispatcher_connect(self.hass, signal, _handle_door)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

# ------------------------
# Last Door Log sensor (per door) + RestoreEntity
# ------------------------
class ProtectorDoorLastLogSensor(SensorEntity, RestoreEntity):
    """
    Minimal, friendly "Last Door Log" with stable attributes.

    State rules:
      - READER_ACCESS_GRANTED  -> "<Name> granted access"
      - READER_ACCESS_DENIED   -> "<Name> denied access"
      - USER_ACCESS_GRANTED    -> "<Name> granted access"
      - USER_ACCESS_DENIED     -> "<Name> denied access"
      - ACTIONPLAN_MESSAGE/STATE like "Home Assistant unlocked Door" -> "<Name> unlocked/locked"
      - DOOR_LOCK_STATE does NOT change state (we only update Door Message)
    """

    _attr_should_poll = False

    _READER_TYPES = {
        "READER_ACCESS_GRANTED",
        "READER_ACCESS_DENIED",
        "USER_ACCESS_GRANTED",
        "USER_ACCESS_DENIED",
    }

    _AP_TYPES = {
        "ACTIONPLAN_MESSAGE",
        "ACTIONPLAN_STATE",
    }

    _DOOR_STATE_TYPES = {
        "DOOR_LOCK_STATE",
    }

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        base_url: str,
        door_id: int,
        door_name: str,
        desc: ProtectorDoorDesc,
    ) -> None:
        self.hass = hass
        self._entry_id = entry_id
        self._base_url = base_url
        self._door_id = int(door_id)
        self._door_name = door_name
        self.entity_description = desc
        host = base_url.split("://", 1)[1]

        self._attr_name = f"{door_name} {desc.name or desc.key}"
        self._attr_unique_id = f"{DOMAIN}_{host}_door_{door_id}_{desc.key}|{entry_id}"

        self._attr_native_value: StateType = None  # "<Name> granted/denied access" or "<Name> unlocked/locked"

        # Fixed, always-present attributes
        self._attrs: Dict[str, Any] = {
            "Reader Message": None,        # last GRANTED/DENIED (or action-plan) line
            "Reader Message Time": None,   # timestamp for that line
            "Door Message": None,          # last "Door ... Is Now Unlocked/Locked"
            "Door ID": self._door_id,
            "Partition ID": None,
        }
        self._unsub: Optional[Callable[[], None]] = None

    @property
    def device_info(self):
        host = self._base_url.split("://", 1)[1]
        return {
            "identifiers": {(DOMAIN, f"door:{host}:{self._door_id}|{self._entry_id}")},
            "manufacturer": "Hartmann Controls",
            "model": "Protector.Net Door",
            "name": self._door_name,
            "configuration_url": self._base_url,
            "via_device": (DOMAIN, f"hub:{host}|{self._entry_id}"),
        }

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        return dict(self._attrs)

    @staticmethod
    def _extract_name_for_reader_line(message: str) -> Optional[str]:
        if not message:
            return None
        m = re.match(r"^(?P<name>.+?)\s+(Granted|Denied)\s+Access\b", message, flags=re.IGNORECASE)
        if m:
            return m.group("name").strip()
        return None

    @staticmethod
    def _extract_name_for_action_line(message: str) -> Optional[str]:
        if not message:
            return None
        m = re.match(r"^(?P<name>.+?)\s+(unlocked|locked)\b", message, flags=re.IGNORECASE)
        if m:
            return m.group("name").strip()
        return None

    @staticmethod
    def _is_unlock_msg(message_lc: str) -> bool:
        return " unlocked " in f" {message_lc} "

    @staticmethod
    def _is_lock_msg(message_lc: str) -> bool:
        return " locked " in f" {message_lc} "

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # Restore last state & attributes
        last = await self.async_get_last_state()
        if last:
            if last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE, None, ""):
                self._attr_native_value = last.state
            la = last.attributes or {}
            # Keep only our fixed keys; ignore anything else from older versions
            for key in ("Reader Message", "Reader Message Time", "Door Message", "Door ID", "Partition ID"):
                if key in la:
                    self._attrs[key] = la[key]
            # ensure Door ID is correct even if entry_id changed
            self._attrs["Door ID"] = self._door_id
            self.async_write_ha_state()

        signal = f"{DISPATCH_LOG}_{self._entry_id}"

        @callback
        def _handle_log(evt: dict[str, Any]) -> None:
            if int(evt.get("door_id")) != self._door_id:
                return

            msg: str = evt.get("log") or ""
            raw: dict = evt.get("raw") or {}
            ntype: str = (evt.get("notification_type") or raw.get("NotificationType") or "").upper()
            ts: str = (evt.get("timestamp") or raw.get("Date") or "") or None

            # Keep Door ID always, keep Partition ID current
            self._attrs["Door ID"] = self._door_id
            self._attrs["Partition ID"] = evt.get("partition_id")

            msg_l = msg.lower()

            # --- Reader GRANTED/DENIED: set state & reader attributes ---
            if ntype in {"READER_ACCESS_GRANTED", "READER_ACCESS_DENIED", "USER_ACCESS_GRANTED", "USER_ACCESS_DENIED"}:
                who = self._extract_name_for_reader_line(msg) or (evt.get("source") or {}).get("name") or raw.get("SourceName")
                if who:
                    if "granted access" in msg_l:
                        self._attr_native_value = f"{who} granted access"
                    elif "denied access" in msg_l:
                        self._attr_native_value = f"{who} denied access"
                    else:
                        self._attr_native_value = f"{who} " + ("granted access" if "granted" in msg_l else "denied access" if "denied" in msg_l else "event")

                self._attrs["Reader Message"] = msg
                self._attrs["Reader Message Time"] = ts
                self.async_write_ha_state()
                return

            # --- Action plan messages: set state like a 'reader' event for UI ---
            if ntype in {"ACTIONPLAN_MESSAGE", "ACTIONPLAN_STATE"}:
                who = self._extract_name_for_action_line(msg) or (evt.get("source") or {}).get("name") or raw.get("SourceName")
                if who:
                    if self._is_unlock_msg(msg_l):
                        self._attr_native_value = f"{who} unlocked"
                    elif self._is_lock_msg(msg_l):
                        self._attr_native_value = f"{who} locked"
                    else:
                        self._attr_native_value = who

                # Treat the AP line as the "Reader Message"
                self._attrs["Reader Message"] = msg or (f"{who} action" if who else None)
                self._attrs["Reader Message Time"] = ts
                self.async_write_ha_state()
                return

            # --- Door state messages: update "Door Message" only ---
            if ntype == "DOOR_LOCK_STATE":
                if "door " in msg_l and (" unlocked" in msg_l or " locked" in msg_l):
                    self._attrs["Door Message"] = msg
                self.async_write_ha_state()
                return

            # Other/unknown types: store door lock text if it obviously looks like one
            if "door " in msg_l and (" unlocked" in msg_l or " locked" in msg_l):
                self._attrs["Door Message"] = msg
                self.async_write_ha_state()
                return

            # Otherwise ignore silently
            return

        self._unsub = async_dispatcher_connect(self.hass, signal, _handle_log)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

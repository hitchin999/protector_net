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
from .services import DISPATCH_TEMP_CODE, DISPATCH_OTR

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

TEMP_CODE_DESC = ProtectorDoorDesc(
    key="temp_code",
    name="Temp Code",
)

OTR_SCHEDULES_DESC = ProtectorDoorDesc(
    key="otr_schedules",
    name="OTR Schedules",
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

        # Derive site filter from title if not explicitly set
        if not site_name_contains:
            if "–" in (entry.title or ""):
                site_name_contains = (entry.title or "").split("–", 1)[1].strip()
            elif "-" in (entry.title or ""):
                site_name_contains = (entry.title or "").split("-", 1)[-1].strip()

        # >>> Fix: ignore the no-op/default label so we don't filter everything out
        if site_name_contains and site_name_contains.strip().lower() == "default partition":
            _LOGGER.debug("[%s] Ignoring site filter 'Default Partition' (treating as no filter)", entry.entry_id)
            site_name_contains = None
        # <<<

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
            entities.append(ProtectorDoorTempCodeSensor(hass, entry.entry_id, base_url, did, dname, TEMP_CODE_DESC))
            entities.append(ProtectorDoorOTRSensor(hass, entry.entry_id, base_url, did, dname, OTR_SCHEDULES_DESC))

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
                "system_type": la.get("system_type"),
            }
            self.async_write_ha_state()

        signal = f"{DISPATCH_HUB}_{self._entry_id}"

        @callback
        def _hub_evt(data: dict[str, Any]) -> None:
            # Map capability → friendly system type
            supp = data.get("supports_status_snapshot")
            system_type = (
                "Odyssey" if supp is True
                else "ProtectorNET" if supp is False
                else "Unknown"
            )

            # Keep these **only** (plus system_type):
            self._last_attrs = {
                "phase": data.get("phase"),
                "connected": data.get("connected"),
                "mapped_doors": data.get("mapped_doors"),
                "partition_id": self._partition_id,
                "system_type": system_type,
            }
            self._attr_native_value = "running" if data.get("connected") else (data.get("phase") or "idle")
            self.async_write_ha_state()

        self._unsub = async_dispatcher_connect(self.hass, signal, _hub_evt)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None


# ------------------------
# OTR Schedules sensor - shows OneTimeRun schedules per door
# ------------------------
from datetime import timedelta

OTR_UPDATE_INTERVAL = timedelta(minutes=5)


class ProtectorDoorOTRSensor(SensorEntity, RestoreEntity):
    """Shows OTR (One Time Run) schedules for a specific door from Hartmann."""

    _attr_should_poll = False  # We'll use our own timer

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

        self._attr_native_value: StateType = 0
        self._attr_unit_of_measurement = "schedules"
        self._attr_icon = "mdi:calendar-clock"
        
        self._schedules: List[Dict[str, Any]] = []
        self._last_updated: Optional[str] = None
        self._unsub_timer: Optional[Callable[[], None]] = None
        self._unsub_otr: Optional[Callable[[], None]] = None

    @property
    def device_info(self):
        host = self._base_url.split("://", 1)[1]
        return {
            "identifiers": {(DOMAIN, f"door:{host}:{self._door_id}|{self._entry_id}")},
            "manufacturer": "Yoel Goldstein/Vaayer LLC",
            "model": "Protector.Net Door",
            "name": self._door_name,
            "configuration_url": self._base_url,
            "via_device": (DOMAIN, f"hub:{host}|{self._entry_id}"),
        }

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        from datetime import datetime
        
        # Separate active vs upcoming
        now = datetime.now().isoformat()
        active = []
        upcoming = []
        
        for s in self._schedules:
            start = s.get("start_time", "")
            stop = s.get("stop_time", "")
            
            schedule_info = {
                "id": s.get("id"),
                "name": s.get("name"),
                "mode": s.get("mode"),
                "start": start,
                "stop": stop,
            }
            
            # Simple comparison (ISO format strings compare correctly)
            if start <= now <= stop:
                active.append(schedule_info)
            elif start > now:
                upcoming.append(schedule_info)
        
        return {
            "active_schedules": active,
            "upcoming_schedules": upcoming,
            "all_schedules": self._schedules,
            "total_count": len(self._schedules),
            "active_count": len(active),
            "upcoming_count": len(upcoming),
            "last_updated": self._last_updated,
            "door_id": self._door_id,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # Restore last state
        last = await self.async_get_last_state()
        if last:
            if last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE, None, ""):
                try:
                    self._attr_native_value = int(last.state)
                except (ValueError, TypeError):
                    self._attr_native_value = 0
            la = last.attributes or {}
            self._schedules = la.get("all_schedules", [])
            self._last_updated = la.get("last_updated")
            self.async_write_ha_state()
        
        # Initial fetch
        await self._async_fetch_schedules()
        
        # Schedule periodic updates every 5 minutes
        from homeassistant.helpers.event import async_track_time_interval
        
        async def _scheduled_update(_now):
            await self._async_fetch_schedules()
        
        self._unsub_timer = async_track_time_interval(
            self.hass, _scheduled_update, OTR_UPDATE_INTERVAL
        )
        
        # Listen for immediate OTR update signals (fired by create/delete services)
        @callback
        def _handle_otr_signal(data=None):
            self.hass.async_create_task(self._async_fetch_schedules())
        
        signal = f"{DISPATCH_OTR}_{self._entry_id}"
        self._unsub_otr = async_dispatcher_connect(self.hass, signal, _handle_otr_signal)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
        if self._unsub_otr:
            self._unsub_otr()
            self._unsub_otr = None

    async def _async_fetch_schedules(self) -> None:
        """Fetch OTR schedules for this door from Hartmann."""
        from . import api
        from datetime import datetime
        
        try:
            # Get all schedules from API
            all_schedules = await api.get_one_time_runs(self.hass, self._entry_id)
            
            # Filter to only schedules for this door
            door_schedules = []
            for s in all_schedules:
                door_ids = s.get("door_ids", [])
                if self._door_id in door_ids:
                    door_schedules.append(s)
                elif not door_ids and s.get("door_name") == self._door_name:
                    # Fallback: match by door name if door_ids couldn't be resolved
                    door_schedules.append(s)
            
            self._schedules = door_schedules
            self._attr_native_value = len(door_schedules)
            self._last_updated = datetime.now().isoformat()
            self.async_write_ha_state()
            
            _LOGGER.debug(
                "[%s] Updated OTR schedules for door %d: %d schedules",
                self._entry_id, self._door_id, len(door_schedules)
            )
        except Exception as e:
            _LOGGER.warning(
                "[%s] Failed to fetch OTR schedules for door %d: %s",
                self._entry_id, self._door_id, e
            )

    async def async_update(self) -> None:
        """Manual refresh support."""
        await self._async_fetch_schedules()


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
            "manufacturer": "Yoel Goldstein/Vaayer LLC",
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
      - OTR events ("One Time Run Time Zone Changed to Mode X") -> "OTR Unlock @ time"
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
            "manufacturer": "Yoel Goldstein/Vaayer LLC",
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
                    # Format time for state (e.g., "@ 1:06 AM")
                    time_suffix = ""
                    if ts:
                        try:
                            from datetime import datetime
                            # Try parsing common formats
                            for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%B %d, %Y at %I:%M:%S %p"]:
                                try:
                                    dt = datetime.strptime(ts[:26] if 'T' in ts else ts, fmt)
                                    time_suffix = f" @ {dt.strftime('%-I:%M %p')}"
                                    break
                                except ValueError:
                                    continue
                            if not time_suffix:
                                # Fallback: use current time
                                time_suffix = f" @ {datetime.now().strftime('%-I:%M %p')}"
                        except Exception:
                            pass
                    
                    if "granted access" in msg_l:
                        self._attr_native_value = f"{who} granted access{time_suffix}"
                    elif "denied access" in msg_l:
                        self._attr_native_value = f"{who} denied access{time_suffix}"
                    else:
                        self._attr_native_value = f"{who} " + ("granted access" if "granted" in msg_l else "denied access" if "denied" in msg_l else "event") + time_suffix

                self._attrs["Reader Message"] = msg
                self._attrs["Reader Message Time"] = ts
                self.async_write_ha_state()
                return

            # --- Action plan messages: set state like a 'reader' event for UI ---
            if ntype in {"ACTIONPLAN_MESSAGE", "ACTIONPLAN_STATE"}:
                who = self._extract_name_for_action_line(msg) or (evt.get("source") or {}).get("name") or raw.get("SourceName")
                if who:
                    # Format time for state (e.g., "@ 1:06 AM")
                    time_suffix = ""
                    if ts:
                        try:
                            from datetime import datetime
                            for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%B %d, %Y at %I:%M:%S %p"]:
                                try:
                                    dt = datetime.strptime(ts[:26] if 'T' in ts else ts, fmt)
                                    time_suffix = f" @ {dt.strftime('%-I:%M %p')}"
                                    break
                                except ValueError:
                                    continue
                            if not time_suffix:
                                time_suffix = f" @ {datetime.now().strftime('%-I:%M %p')}"
                        except Exception:
                            pass
                    
                    if self._is_unlock_msg(msg_l):
                        self._attr_native_value = f"{who} unlocked{time_suffix}"
                    elif self._is_lock_msg(msg_l):
                        self._attr_native_value = f"{who} locked{time_suffix}"
                    else:
                        self._attr_native_value = f"{who}{time_suffix}"

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

            # --- OTR (One Time Run) events: update state + Door Message ---
            if "one time run" in msg_l:
                # Message like: "Door Gate Front Door One Time Run Time Zone Changed to Mode Unlock"
                time_suffix = ""
                if ts:
                    try:
                        from datetime import datetime
                        for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%B %d, %Y at %I:%M:%S %p"]:
                            try:
                                dt = datetime.strptime(ts[:26] if 'T' in ts else ts, fmt)
                                time_suffix = f" @ {dt.strftime('%-I:%M %p')}"
                                break
                            except ValueError:
                                continue
                        if not time_suffix:
                            time_suffix = f" @ {datetime.now().strftime('%-I:%M %p')}"
                    except Exception:
                        pass
                
                # Extract mode from "Changed to Mode <Mode>"
                mode_match = re.search(r"changed to mode\s+(\w+)", msg_l, flags=re.IGNORECASE)
                mode_str = mode_match.group(1).title() if mode_match else "OTR"
                
                self._attr_native_value = f"OTR {mode_str}{time_suffix}"
                self._attrs["Reader Message"] = msg
                self._attrs["Reader Message Time"] = ts
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


# ------------------------
# Temp Code sensor (per door) + RestoreEntity
# ------------------------
class ProtectorDoorTempCodeSensor(SensorEntity, RestoreEntity):
    """
    Sensor tracking temporary access codes for a door.
    
    State: Last created code (or "None" if no codes)
    Attributes:
      - active_codes: List of all active codes with names
      - last_code_name: Name of the last created code
      - last_code_created: Timestamp of last code creation
    """

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

        self._attr_name = f"{door_name} {desc.name or desc.key}"
        self._attr_unique_id = f"{DOMAIN}_{host}_door_{door_id}_{desc.key}|{entry_id}"

        self._attr_native_value: StateType = "None"

        # Attributes for tracking codes
        self._attrs: Dict[str, Any] = {
            "active_codes": [],  # List of {"code_name": str, "code": str, "user_id": int}
            "last_code_name": None,
            "last_code_created": None,
            "door_id": self._door_id,
        }
        self._unsub: Optional[Callable[[], None]] = None

    @property
    def device_info(self):
        host = self._base_url.split("://", 1)[1]
        return {
            "identifiers": {(DOMAIN, f"door:{host}:{self._door_id}|{self._entry_id}")},
            "manufacturer": "Yoel Goldstein/Vaayer LLC",
            "model": "Protector.Net Door",
            "name": self._door_name,
            "configuration_url": self._base_url,
            "via_device": (DOMAIN, f"hub:{host}|{self._entry_id}"),
        }

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        return dict(self._attrs)

    @property
    def icon(self) -> str:
        """Return an icon based on whether codes exist."""
        if self._attrs.get("active_codes"):
            return "mdi:key-plus"
        return "mdi:key-outline"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # Restore last state & attributes
        last = await self.async_get_last_state()
        if last:
            if last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE, None, ""):
                self._attr_native_value = last.state
            la = last.attributes or {}
            for key in ("active_codes", "last_code_name", "last_code_created", "door_id"):
                if key in la:
                    self._attrs[key] = la[key]
            # Ensure door_id is correct
            self._attrs["door_id"] = self._door_id
            self.async_write_ha_state()

        signal = f"{DISPATCH_TEMP_CODE}_{self._entry_id}"

        @callback
        def _handle_temp_code(evt: dict[str, Any]) -> None:
            """Handle temp code create/delete events from services."""
            if int(evt.get("door_id")) != self._door_id:
                return

            action = evt.get("action")
            
            if action == "create":
                code = evt.get("code")
                code_name = evt.get("code_name")
                user_id = evt.get("user_id")
                
                # Update state to the new code
                self._attr_native_value = code
                
                # Add to active codes list
                active_codes = list(self._attrs.get("active_codes", []))
                
                # Add the new code
                new_entry = {
                    "code_name": code_name,
                    "code": code,
                    "user_id": user_id,
                    "start_time": evt.get("start_time"),
                    "end_time": evt.get("end_time"),
                }
                active_codes.append(new_entry)
                
                # Update attributes
                self._attrs["active_codes"] = active_codes
                self._attrs["last_code_name"] = code_name
                self._attrs["last_code_created"] = evt.get("timestamp") or None
                
                _LOGGER.debug(
                    "[%s] Door %d: Created temp code '%s': %s",
                    self._entry_id, self._door_id, code_name, code
                )
                
            elif action == "delete":
                code = evt.get("code")
                
                # Remove from active codes list
                active_codes = list(self._attrs.get("active_codes", []))
                active_codes = [c for c in active_codes if c.get("code") != code]
                self._attrs["active_codes"] = active_codes
                
                # Update state to the most recent remaining code or "None"
                if active_codes:
                    self._attr_native_value = active_codes[-1].get("code")
                else:
                    self._attr_native_value = "None"
                
                _LOGGER.debug(
                    "[%s] Door %d: Deleted temp code: %s",
                    self._entry_id, self._door_id, code
                )
            
            elif action == "update":
                update_name = evt.get("code_name")
                active_codes = list(self._attrs.get("active_codes", []))
                for entry in active_codes:
                    if entry.get("code_name") == update_name:
                        if evt.get("end_time") is not None:
                            entry["end_time"] = evt["end_time"]
                        if evt.get("start_time") is not None:
                            entry["start_time"] = evt["start_time"]
                        break
                self._attrs["active_codes"] = active_codes
                
                _LOGGER.debug(
                    "[%s] Door %d: Updated temp code '%s'",
                    self._entry_id, self._door_id, update_name
                )
            
            self.async_write_ha_state()

        self._unsub = async_dispatcher_connect(self.hass, signal, _handle_temp_code)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

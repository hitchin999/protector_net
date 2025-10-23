# custom_components/protector_net/__init__.py

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, UI_STATE
from .ws import SignalRClient
from . import api

_LOGGER = logging.getLogger(DOMAIN)

# Platforms we expose
# (Old per-action buttons are going away except Pulse Unlock; new controls live in select/number/switch.)
PLATFORMS: list[str] = ["button", "sensor", "select", "number", "switch"]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration (domain) once."""
    hass.data.setdefault(DOMAIN, {})
    _LOGGER.debug("async_setup for %s initialized", DOMAIN)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Register a single config entry WITHOUT blocking HA startup."""
    base_url: str = entry.data["base_url"]
    host = base_url.split("://", 1)[1]

    # Persist runtime config for this entry_id (no I/O here)
    data: dict[str, Any] = {
        "base_url": base_url,
        "username": entry.data["username"],
        "password": entry.data["password"],
        "session_cookie": entry.data["session_cookie"],
        "partition_id": entry.data["partition_id"],
        "host": host,
        "hub_identifier": f"hub:{host}|{entry.entry_id}",
        "verify_ssl": bool(entry.options.get("verify_ssl", False)),
        "override_minutes": entry.options.get(
            "override_minutes", entry.data.get("override_minutes")
        ),
        # New: shared, ephemeral per-door UI state used by select/number/switch
        UI_STATE: {},  # {door_id: {"type": str, "mode": str, "minutes": int}}
        # New: cached legend for DoorTimeZoneMode to sync Override Mode select from WS
        "tz_index_to_name": {},   # {int: "Card or Pin", ...} (normalized by us)
        "tz_name_to_index": {},   # {"card or pin": 3, ...}   (normalized key)
    }
    hass.data[DOMAIN][entry.entry_id] = data

    # Make options changes trigger a reload
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    async def _deferred_start(_event=None) -> None:
        """Actually start hub + platforms after HA has started."""
        # Cache the DoorTimeZoneMode legend (best effort; we’ll retry later if needed)
        try:
            tz_map = await api.get_door_time_zone_states(hass, entry.entry_id)
            # Normalize names to user-friendly form & lowercase keys for reverse map
            name_by_idx: dict[int, str] = {}
            idx_by_name: dict[str, int] = {}
            for idx, item in tz_map.items():
                raw = str(item.get("name") or "")
                # Normalize common variants produced by servers (“CardOrPin”, “Card Or Pin”, etc.)
                nice = (
                    raw.replace("And", "and")
                       .replace("Or", "or")
                       .replace("Credential", "Credential")
                       .strip()
                )
                # Guard: ensure Unlock capitalization matches our UI
                if nice.lower() == "unlock":
                    nice = "Unlock"
                name_by_idx[int(idx)] = nice
                idx_by_name[nice.lower()] = int(idx)

            hass.data[DOMAIN][entry.entry_id]["tz_index_to_name"] = name_by_idx
            hass.data[DOMAIN][entry.entry_id]["tz_name_to_index"] = idx_by_name
            _LOGGER.debug("[%s] Loaded DoorTimeZoneMode legend: %s", entry.entry_id, name_by_idx)
        except Exception as e:
            _LOGGER.debug("[%s] Could not load DoorTimeZoneMode legend yet: %s", entry.entry_id, e)

        # Start SignalR hub (non-blocking)
        hub = SignalRClient(hass, entry.entry_id)
        hass.data[DOMAIN][entry.entry_id]["hub"] = hub
        hub.async_start()
        _LOGGER.debug("[%s] Hub started for %s", entry.entry_id, host)

        # Now set up platforms (these return quickly; our platforms offload I/O)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        _LOGGER.debug("[%s] Platforms set up: %s", entry.entry_id, ", ".join(PLATFORMS))

    # If HA already running (entry added later), start immediately; otherwise wait for STARTED.
    if hass.is_running:
        hass.async_create_task(_deferred_start())
    else:
        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _deferred_start)
        )

    # Critical: return True right away so HA can finish booting
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a single config entry cleanly."""
    # Stop the hub first (so WS closes immediately)
    hub: SignalRClient | None = hass.data[DOMAIN].get(entry.entry_id, {}).get("hub")
    if hub:
        await hub.async_stop()
        _LOGGER.debug("[%s] Hub stopped", entry.entry_id)

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        _LOGGER.debug("[%s] Entry data cleared", entry.entry_id)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options updates by reloading the entry."""
    _LOGGER.debug("[%s] Options updated; reloading entry", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)

"""
Managed Door Schedules - lifecycle orchestration.

Layers on top of api.py to handle the four lifecycle operations:

    Unmanaged ────► (provision)  ────► Managed ────► (activate)  ────► Active
       ▲                                  │                              │
       │                                  ▼                              ▼
       └──── (unprovision) ◄── (deactivate) ◄────────────────────────────┘

States are stored in entry.options[KEY_MANAGED_DOORS] as a dict keyed by
door_id (string). See const.py for the schema.

Concurrency note: every call mutates entry.options via async_update_entry and
runs Hartmann API calls. Callers should serialize bulk ops in the order
Active->Managed (deactivate) and Managed->Unmanaged (delete TZ) to avoid
trying to delete a TZ a door still points at.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry

from . import api
from .const import (
    DOMAIN,
    KEY_MANAGED_DOORS,
    DEFAULT_SCHEDULE_MODE,
    SCHEDULE_MODES,
)

_LOGGER = logging.getLogger(f"{DOMAIN}.managed_schedules")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_managed(entry: ConfigEntry) -> dict[str, dict[str, Any]]:
    """Return the managed-doors dict from entry options (always a dict)."""
    raw = entry.options.get(KEY_MANAGED_DOORS) or {}
    # JSON serialization can leave us with int-ish keys; normalize to str.
    return {str(k): dict(v) for k, v in raw.items()}


def is_managed(entry: ConfigEntry, door_id: int) -> bool:
    return str(door_id) in _get_managed(entry)


def get_managed_info(entry: ConfigEntry, door_id: int) -> dict[str, Any] | None:
    return _get_managed(entry).get(str(door_id))


# ---------------------------------------------------------------------------
# Lifecycle ops
# ---------------------------------------------------------------------------

async def provision_door(
    hass: HomeAssistant,
    entry: ConfigEntry,
    door_id: int,
    door_name: str,
) -> dict[str, Any]:
    """Provision (Unmanaged -> Managed).

    Creates the HA DoorTimeZone in Hartmann and records the door's CURRENT
    DoorTimeZoneId so we can roll back later. The door itself is NOT yet
    repointed at the HA TZ — that happens on activate_door.

    Returns {"success": bool, "error"?: str, "managed_info"?: dict}
    """
    entry_id = entry.entry_id
    if is_managed(entry, door_id):
        return {"success": True, "managed_info": get_managed_info(entry, door_id),
                "note": "already managed"}

    # Capture the door's current TZ before doing anything else.
    # NOTE: GET /api/Doors/{Id} returns DoorBase wrapped in {Result} which
    # LACKS DoorTimeZoneId, so api.get_door_with_tz uses the list endpoint
    # instead. The list returns the full Door schema with DoorTimeZoneId.
    door = await api.get_door_with_tz(hass, entry_id, door_id)
    if not door:
        return {"success": False, "error": f"Could not fetch door {door_id}"}
    original_tz_id = door.get("DoorTimeZoneId")
    if original_tz_id is None:
        return {"success": False, "error": f"Door {door_id} has no DoorTimeZoneId"}

    # Create the HA TZ in Hartmann.
    initial_mode = DEFAULT_SCHEDULE_MODE
    result = await api.provision_managed_tz(
        hass, entry_id, door_id, door_name, initial_mode,
    )
    if not result:
        return {"success": False, "error": "Failed to create HA DoorTimeZone in Hartmann"}

    info = {
        "ha_tz_id":       int(result["id"]),
        "ha_tz_name":     str(result["name"]),
        "original_tz_id": int(original_tz_id),
        "active":         False,
        "current_mode":   initial_mode,
    }
    _LOGGER.info(
        "%s: Provisioned door %s (%s): HA TZ %s, original TZ %s",
        entry_id, door_id, door_name, info["ha_tz_id"], info["original_tz_id"],
    )
    return {"success": True, "managed_info": info}


async def unprovision_door(
    hass: HomeAssistant,
    entry: ConfigEntry,
    door_id: int,
) -> dict[str, Any]:
    """Unprovision (Managed -> Unmanaged).

    If the door is currently Active (pointing at the HA TZ), repoints it back
    to its original TZ first. Then deletes the HA TZ from Hartmann.

    Caller is responsible for firing Update Panels afterward if the door was
    active.
    """
    entry_id = entry.entry_id
    info = get_managed_info(entry, door_id)
    if not info:
        return {"success": True, "note": "not managed"}

    # Step 1: if active, repoint door back to its original TZ.
    if info.get("active"):
        ok = await api.set_door_time_zone_id(
            hass, entry_id, door_id, int(info["original_tz_id"]),
        )
        if not ok:
            return {
                "success": False,
                "error": (
                    f"Could not repoint door {door_id} back to original TZ "
                    f"{info['original_tz_id']}; refusing to delete HA TZ"
                ),
            }

    # Step 2: delete the HA TZ. Best-effort: log but don't fail the whole op
    # if this fails (the TZ is now orphaned but harmless and easy to clean
    # later via find_orphan_managed_tzs).
    ha_tz_id = info.get("ha_tz_id")
    if ha_tz_id:
        deleted = await api._delete_door_time_zone(hass, entry_id, int(ha_tz_id))
        if not deleted:
            _LOGGER.warning(
                "%s: HA TZ %s for door %s could not be deleted; leaving as orphan",
                entry_id, ha_tz_id, door_id,
            )

    _LOGGER.info("%s: Unprovisioned door %s", entry_id, door_id)
    return {"success": True, "was_active": bool(info.get("active"))}


async def activate_door(
    hass: HomeAssistant,
    entry: ConfigEntry,
    door_id: int,
) -> dict[str, Any]:
    """Activate (Managed -> Active). Flips door.DoorTimeZoneId to the HA TZ.

    Caller fires Update Panels (debounced across multiple doors).
    """
    entry_id = entry.entry_id
    info = get_managed_info(entry, door_id)
    if not info:
        return {"success": False, "error": f"Door {door_id} is not managed"}
    if info.get("active"):
        return {"success": True, "note": "already active"}

    ok = await api.set_door_time_zone_id(
        hass, entry_id, door_id, int(info["ha_tz_id"]),
    )
    if not ok:
        return {"success": False, "error": "Failed to update door's DoorTimeZoneId"}

    _LOGGER.info(
        "%s: Activated door %s (now using HA TZ %s)",
        entry_id, door_id, info["ha_tz_id"],
    )
    return {"success": True}


async def deactivate_door(
    hass: HomeAssistant,
    entry: ConfigEntry,
    door_id: int,
) -> dict[str, Any]:
    """Deactivate (Active -> Managed). Repoints door back to original TZ.

    Caller fires Update Panels.
    """
    entry_id = entry.entry_id
    info = get_managed_info(entry, door_id)
    if not info:
        return {"success": False, "error": f"Door {door_id} is not managed"}
    if not info.get("active"):
        return {"success": True, "note": "already inactive"}

    ok = await api.set_door_time_zone_id(
        hass, entry_id, door_id, int(info["original_tz_id"]),
    )
    if not ok:
        return {"success": False, "error": "Failed to repoint door to original TZ"}

    _LOGGER.info(
        "%s: Deactivated door %s (back on original TZ %s)",
        entry_id, door_id, info["original_tz_id"],
    )
    return {"success": True}


async def set_mode(
    hass: HomeAssistant,
    entry: ConfigEntry,
    door_id: int,
    mode: str,
) -> dict[str, Any]:
    """Change the mode of a managed door's HA TZ.

    Idempotent: if the requested mode equals current_mode, this is a no-op.
    If the door is Active, caller should fire Update Panels.

    Note: this does NOT activate an inactive door — it only updates the HA TZ
    so that whenever the user activates the door, the TZ is already at the
    desired mode. This matches the staging UX the user requested.
    """
    entry_id = entry.entry_id
    if mode not in SCHEDULE_MODES:
        return {"success": False, "error": f"Invalid mode: {mode}"}

    info = get_managed_info(entry, door_id)
    if not info:
        return {"success": False, "error": f"Door {door_id} is not managed"}

    if info.get("current_mode") == mode:
        return {"success": True, "changed": False, "note": "already at requested mode"}

    ok = await api.set_managed_tz_mode(
        hass,
        entry_id,
        int(info["ha_tz_id"]),
        str(info["ha_tz_name"]),
        door_id,
        mode,
    )
    if not ok:
        return {"success": False, "error": "Failed to update HA TZ TimeSpans"}

    _LOGGER.info(
        "%s: Door %s mode %s -> %s (active=%s)",
        entry_id, door_id, info.get("current_mode"), mode, info.get("active"),
    )
    return {
        "success": True,
        "changed": True,
        "active":  bool(info.get("active")),
    }


# ---------------------------------------------------------------------------
# Bulk reconcile (used by config_flow on submit)
# ---------------------------------------------------------------------------

async def reconcile(
    hass: HomeAssistant,
    entry: ConfigEntry,
    desired_managed_door_ids: list[int],
    desired_active_door_ids: list[int],
    door_names: dict[int, str],
) -> dict[str, Any]:
    """Reconcile current managed_doors state to a desired set.

    Computes a plan:
      - For each currently-active door not in desired_active: deactivate
      - For each currently-managed door not in desired_managed: unprovision
      - For each newly-desired-managed door: provision
      - For each newly-desired-active door (and already-managed ones now
        flagged active): activate

    Saves the resulting state to entry.options at the end. Fires Update
    Panels exactly once if anything was activated or deactivated.

    Returns a summary dict with per-door results.
    """
    entry_id = entry.entry_id
    current = _get_managed(entry)
    desired_managed = {str(d) for d in desired_managed_door_ids}
    desired_active = {str(d) for d in desired_active_door_ids}

    # Active set must be a subset of managed set (UI should already enforce
    # this, but be defensive).
    desired_active &= desired_managed

    summary: dict[str, Any] = {
        "deactivated": [], "unprovisioned": [],
        "provisioned": [],  "activated":     [],
        "failed":      [],
    }
    panels_dirty = False

    # 1) Deactivate doors that are currently active but should not be.
    for did_str, info in list(current.items()):
        if not info.get("active"):
            continue
        if did_str in desired_active:
            continue
        result = await deactivate_door(hass, entry, int(did_str))
        if result.get("success"):
            current[did_str]["active"] = False
            summary["deactivated"].append(int(did_str))
            panels_dirty = True
        else:
            summary["failed"].append({"door_id": int(did_str), "op": "deactivate",
                                      "error": result.get("error")})

    # 2) Unprovision doors that are currently managed but should not be.
    for did_str in list(current.keys()):
        if did_str in desired_managed:
            continue
        # unprovision_door reads info from entry, but since we haven't saved
        # yet, we patch the local dict via a temporary save? No - we work
        # directly: replicate the logic without going through entry-helpers.
        info = current[did_str]
        # Repoint if active was somehow still true (shouldn't be after step 1)
        if info.get("active"):
            ok = await api.set_door_time_zone_id(
                hass, entry_id, int(did_str), int(info["original_tz_id"]),
            )
            panels_dirty = True
            if not ok:
                summary["failed"].append({"door_id": int(did_str), "op": "unprovision-repoint",
                                          "error": "could not repoint door"})
                continue
        ha_tz_id = info.get("ha_tz_id")
        if ha_tz_id:
            await api._delete_door_time_zone(hass, entry_id, int(ha_tz_id))
        del current[did_str]
        summary["unprovisioned"].append(int(did_str))

    # 3) Provision newly-desired-managed doors.
    for did_str in desired_managed:
        if did_str in current:
            continue
        did = int(did_str)
        result = await provision_door(
            hass, entry, did, door_names.get(did, f"Door {did}"),
        )
        if result.get("success"):
            current[did_str] = result["managed_info"]
            summary["provisioned"].append(did)
        else:
            summary["failed"].append({"door_id": did, "op": "provision",
                                      "error": result.get("error")})

    # 4) Activate doors that should be active but aren't yet.
    for did_str in desired_active:
        info = current.get(did_str)
        if not info:
            continue  # provision step failed
        if info.get("active"):
            continue
        did = int(did_str)
        ok = await api.set_door_time_zone_id(
            hass, entry_id, did, int(info["ha_tz_id"]),
        )
        if ok:
            current[did_str]["active"] = True
            summary["activated"].append(did)
            panels_dirty = True
        else:
            summary["failed"].append({"door_id": did, "op": "activate",
                                      "error": "could not flip DoorTimeZoneId"})

    # Persist final state.
    # NOTE: caller is responsible for saving the returned `managed_doors`
    # back to entry.options. We don't async_update_entry here to avoid
    # double-writes when called from inside an OptionsFlow (which itself
    # writes options on return). cleanup_all bypasses this and saves
    # directly via _save_managed.
    summary["managed_doors"] = current

    # Fire Update Panels exactly once at the end if anything changed at the
    # door level. TZ-only edits don't strictly need a panel push, but cheap
    # to do once.
    if panels_dirty:
        try:
            await api.update_panels(hass, entry_id)
        except Exception as e:
            _LOGGER.warning("%s: Update Panels after reconcile failed: %s", entry_id, e)

    summary["panels_updated"] = panels_dirty
    return summary


async def cleanup_all(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Full cleanup: deactivate every active door, delete every HA TZ.

    Used by async_remove_entry. Best-effort — logs failures but does not
    raise. Also sweeps for orphan HA TZs (description-tagged but missing
    from our managed_doors record) so an interrupted previous run can't
    leave debris.
    """
    entry_id = entry.entry_id
    managed = _get_managed(entry)

    # Repoint and delete known-managed doors.
    for did_str, info in managed.items():
        try:
            if info.get("active"):
                await api.set_door_time_zone_id(
                    hass, entry_id, int(did_str), int(info["original_tz_id"]),
                )
            ha_tz_id = info.get("ha_tz_id")
            if ha_tz_id:
                await api._delete_door_time_zone(hass, entry_id, int(ha_tz_id))
        except Exception as e:
            _LOGGER.warning("%s: cleanup_all door %s: %s", entry_id, did_str, e)

    # Sweep orphans (TZs tagged for this entry but not in our records).
    known_ids = {int(info.get("ha_tz_id"))
                 for info in managed.values() if info.get("ha_tz_id")}
    try:
        for tz in await api.find_orphan_managed_tzs(hass, entry_id):
            tz_id = tz.get("Id")
            if tz_id and int(tz_id) not in known_ids:
                await api._delete_door_time_zone(hass, entry_id, int(tz_id))
    except Exception as e:
        _LOGGER.debug("%s: orphan sweep failed (non-fatal): %s", entry_id, e)

    try:
        await api.update_panels(hass, entry_id)
    except Exception:
        pass

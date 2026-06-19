# custom_components/protector_net/services.py
"""
Hartmann Control Temp Code Services

Services for creating and deleting temporary PIN codes for doors.
Designed for rental/booking management via automations.
"""
from __future__ import annotations

import logging
import random
import string
from datetime import datetime
from typing import Any, Optional

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DOMAIN, UI_STATE, SCHEDULE_MODES

_LOGGER = logging.getLogger(f"{DOMAIN}.services")

# Service names
SERVICE_CREATE_TEMP_CODE = "create_temp_code"
SERVICE_DELETE_TEMP_CODE = "delete_temp_code"
SERVICE_DELETE_TEMP_CODE_BY_NAME = "delete_temp_code_by_name"
SERVICE_UPDATE_TEMP_CODE = "update_temp_code"
SERVICE_UPDATE_PANELS = "update_panels"

# Dispatcher signal for temp code updates
DISPATCH_TEMP_CODE = f"{DOMAIN}_temp_code_update"
DISPATCH_OTR = f"{DOMAIN}_otr_update"
# Fired after a managed-door schedule change so the Hub "Door Schedules"
# sensor refreshes immediately instead of waiting for its poll interval.
DISPATCH_DOOR_SCHEDULES = f"{DOMAIN}_door_schedules_update"

# Helper to accept single device or list of devices
DEVICE_ID_SCHEMA = vol.Any(cv.string, vol.All(cv.ensure_list, [cv.string]))

# Schema for create_temp_code service
SERVICE_CREATE_TEMP_CODE_SCHEMA = vol.Schema(
    {
        vol.Required("door_device_id"): DEVICE_ID_SCHEMA,
        vol.Required("code_name"): cv.string,
        vol.Optional("random_code", default=True): cv.boolean,
        vol.Optional("code_digits"): vol.All(
            vol.Coerce(int), vol.Range(min=4, max=9)
        ),
        vol.Optional("manual_code"): cv.string,
        vol.Optional("start_time"): cv.string,  # ISO datetime string
        vol.Optional("end_time"): cv.string,    # ISO datetime string
    }
)

# Schema for delete_temp_code service
SERVICE_DELETE_TEMP_CODE_SCHEMA = vol.Schema(
    {
        vol.Required("door_device_id"): DEVICE_ID_SCHEMA,
        vol.Required("code"): cv.string,
    }
)

# Schema for delete_temp_code_by_name service
SERVICE_DELETE_TEMP_CODE_BY_NAME_SCHEMA = vol.Schema(
    {
        vol.Required("door_device_id"): DEVICE_ID_SCHEMA,
        vol.Required("code_name"): cv.string,
        vol.Optional("force_remove", default=False): cv.boolean,
    }
)

# Service name for clearing all codes
SERVICE_CLEAR_ALL_TEMP_CODES = "clear_all_temp_codes"

# Schema for clear_all_temp_codes service
SERVICE_CLEAR_ALL_TEMP_CODES_SCHEMA = vol.Schema(
    {
        vol.Required("door_device_id"): DEVICE_ID_SCHEMA,
    }
)

# Service names for managing which doors a temp code applies to
SERVICE_ADD_DOOR_TO_TEMP_CODE = "add_door_to_temp_code"
SERVICE_REMOVE_DOOR_FROM_TEMP_CODE = "remove_door_from_temp_code"

# Schema for add/remove_door_to_temp_code services
# Either `code` (PIN value) or `code_name` must be provided to identify the
# target temp code; `code` is preferred since it's unique per Hartmann user.
def _require_code_identifier(data):
    if not (data.get("code") or data.get("code_name")):
        raise vol.Invalid("Either 'code' or 'code_name' must be provided")
    return data


SERVICE_DOOR_FOR_TEMP_CODE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required("door_device_id"): DEVICE_ID_SCHEMA,
            vol.Optional("code"): cv.string,
            vol.Optional("code_name"): cv.string,
        }
    ),
    _require_code_identifier,
)

# OTR (One Time Run) schedule service names
SERVICE_CREATE_OTR_SCHEDULE = "create_otr_schedule"
SERVICE_DELETE_OTR_SCHEDULE = "delete_otr_schedule"
SERVICE_GET_OTR_SCHEDULES = "get_otr_schedules"

# Schema for update_temp_code service
SERVICE_UPDATE_TEMP_CODE_SCHEMA = vol.Schema(
    {
        vol.Required("door_device_id"): DEVICE_ID_SCHEMA,
        vol.Required("code_name"): cv.string,
        vol.Optional("end_time"): cv.string,
        vol.Optional("start_time"): cv.string,
    }
)

# Valid modes for OTR schedules
OTR_SCHEDULE_MODES = [
    "Lockdown",
    "Card",
    "Pin",
    "CardOrPin",
    "CardAndPin",
    "Unlock",
    "UnlockWithFirstCardIn",
    "DualCard",
]

# Schema for create_otr_schedule service
SERVICE_CREATE_OTR_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("door_device_id"): DEVICE_ID_SCHEMA,
        vol.Required("start_time"): cv.string,
        vol.Required("stop_time"): cv.string,
        vol.Optional("mode", default="Unlock"): vol.In(OTR_SCHEDULE_MODES),
        vol.Optional("name"): cv.string,
        vol.Optional("description"): cv.string,
    }
)

# Schema for delete_otr_schedule service
SERVICE_DELETE_OTR_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional("schedule_id"): vol.Coerce(int),
        vol.Optional("door_device_id"): DEVICE_ID_SCHEMA,
    }
)

# Schema for get_otr_schedules service
SERVICE_GET_OTR_SCHEDULES_SCHEMA = vol.Schema(
    {
        vol.Optional("door_device_id"): DEVICE_ID_SCHEMA,
    }
)

# Override door service names
SERVICE_OVERRIDE_DOOR = "override_door"
SERVICE_RESUME_DOOR = "resume_door"

# Managed door schedules
SERVICE_SET_DOOR_SCHEDULE_MODE = "set_door_schedule_mode"

# Helper validator: a service that accepts EITHER door_entity OR door_device_id
# (or both) must have at least one populated. Used by override_door / resume_door.
def _require_door_target(data):
    if not (data.get("door_entity") or data.get("door_device_id")):
        raise vol.Invalid(
            "Either 'door_entity' or 'door_device_id' must be provided"
        )
    return data


# Reusable selector for either field accepting str or list[str]
DOOR_ENTITY_SCHEMA = vol.Any(cv.string, vol.All(cv.ensure_list, [cv.string]))


SERVICE_SET_DOOR_SCHEDULE_MODE_SCHEMA = vol.Schema(
    {
        # Entity-only — this service is unreleased so no legacy field needed.
        vol.Required("door_entity"): DOOR_ENTITY_SCHEMA,
        vol.Required("mode"): vol.In(SCHEDULE_MODES),
    }
)

# Valid override types
OVERRIDE_TYPES = ["until_resumed", "for_time", "until_schedule"]

# Valid modes for override_door (same as OTR schedules)
OVERRIDE_DOOR_MODES = [
    "Unlock",
    "Lockdown",
    "Card",
    "Pin",
    "CardOrPin",
    "CardAndPin",
    "FirstCredentialIn",
    "DualCredential",
]

# Schema for override_door service
SERVICE_OVERRIDE_DOOR_SCHEMA = vol.All(
    vol.Schema(
        {
            # Either field is acceptable; resolver de-dupes and combines.
            vol.Optional("door_entity"): DOOR_ENTITY_SCHEMA,
            vol.Optional("door_device_id"): DEVICE_ID_SCHEMA,
            vol.Optional("mode", default="Unlock"): vol.In(OVERRIDE_DOOR_MODES),
            vol.Optional("override_type", default="until_resumed"): vol.In(OVERRIDE_TYPES),
            vol.Optional("minutes"): vol.All(vol.Coerce(int), vol.Range(min=1)),
            vol.Optional("until"): cv.string,  # ISO datetime — auto-computes minutes
        }
    ),
    _require_door_target,
)

# Schema for resume_door service
SERVICE_RESUME_DOOR_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional("door_entity"): DOOR_ENTITY_SCHEMA,
            vol.Optional("door_device_id"): DEVICE_ID_SCHEMA,
        }
    ),
    _require_door_target,
)


def generate_random_code(digits: int = 6) -> str:
    """Generate a random numeric PIN code."""
    return "".join(random.choices(string.digits, k=digits))


def _get_door_id_from_device(hass: HomeAssistant, device_id: str) -> tuple[str | None, int | None]:
    """
    Extract entry_id and door_id from a Protector.Net door device.
    
    Device identifier format: (DOMAIN, "door:{host}:{door_id}|{entry_id}")
    
    Returns (entry_id, door_id) or (None, None) if not found.
    """
    from homeassistant.helpers import device_registry as dr
    
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(device_id)
    
    if not device:
        _LOGGER.error("Device %s not found", device_id)
        return None, None
    
    # Find the protector_net identifier
    for identifier in device.identifiers:
        if identifier[0] == DOMAIN and identifier[1].startswith("door:"):
            # Format: door:{host}:{door_id}|{entry_id}
            try:
                # Remove "door:" prefix
                rest = identifier[1][5:]  # Skip "door:"
                # Split by pipe to get entry_id
                parts = rest.split("|")
                if len(parts) != 2:
                    continue
                entry_id = parts[1]
                # Parse host:door_id from first part
                host_and_door = parts[0]  # {host}:{door_id}
                # Door ID is after the last colon
                door_id_str = host_and_door.rsplit(":", 1)[1]
                door_id = int(door_id_str)
                return entry_id, door_id
            except (ValueError, IndexError) as e:
                _LOGGER.debug("Failed to parse device identifier %s: %s", identifier, e)
                continue
    
    _LOGGER.error("Device %s has no valid Protector.Net door identifier", device_id)
    return None, None


def _get_door_id_from_entity(hass: HomeAssistant, entity_id: str) -> tuple[str | None, int | None]:
    """
    Extract entry_id and door_id from a Protector.Net door entity.
    
    Returns (entry_id, door_id) or (None, None) if not found.
    """
    ent_reg = er.async_get(hass)
    entity = ent_reg.async_get(entity_id)
    
    if not entity:
        _LOGGER.error("Entity %s not found", entity_id)
        return None, None
    
    if entity.platform != DOMAIN:
        _LOGGER.error("Entity %s is not a Protector.Net entity", entity_id)
        return None, None
    
    # The unique_id format is: {DOMAIN}_{host}_door_{door_id}_{key}|{entry_id}
    # OR for temp_code sensor: {DOMAIN}_{host}_door_{door_id}_temp_code|{entry_id}
    unique_id = entity.unique_id
    if not unique_id:
        _LOGGER.error("Entity %s has no unique_id", entity_id)
        return None, None
    
    try:
        # Split by pipe to get entry_id
        parts = unique_id.split("|")
        if len(parts) != 2:
            _LOGGER.error("Invalid unique_id format for %s: %s", entity_id, unique_id)
            return None, None
        
        entry_id = parts[1]
        
        # Parse door_id from the first part
        # Format: protector_net_{host}_door_{door_id}_{sensor_key}
        prefix = unique_id.split("|")[0]
        
        # Find "_door_" and extract the number after it
        door_marker = "_door_"
        if door_marker not in prefix:
            _LOGGER.error("Could not find door marker in unique_id: %s", unique_id)
            return None, None
        
        after_door = prefix.split(door_marker)[1]
        # Door ID is the numeric part before the next underscore
        door_id_str = after_door.split("_")[0]
        door_id = int(door_id_str)
        
        return entry_id, door_id
        
    except (ValueError, IndexError) as e:
        _LOGGER.error("Failed to parse entity unique_id %s: %s", unique_id, e)
        return None, None


def _normalize_device_ids(device_ids: str | list[str]) -> list[str]:
    """Normalize device_ids to always be a list."""
    if isinstance(device_ids, str):
        return [device_ids]
    return list(device_ids)


def _resolve_door_targets(
    hass: HomeAssistant,
    call: ServiceCall,
) -> tuple[dict[str, list[int]], list[str], list[str]]:
    """Resolve `door_entity` and/or `door_device_id` from a ServiceCall to
    a per-entry list of door_ids.

    Both fields are optional individually but the schema requires at least
    one (via _require_door_target). Targets from both are merged and
    de-duplicated by (entry_id, door_id).

    Returns:
        (doors_by_entry, invalid_entities, invalid_devices)

        doors_by_entry: {entry_id: [door_id, ...]}  — order preserved within
        invalid_entities: entity_ids that couldn't be resolved
        invalid_devices:  device_ids that couldn't be resolved
    """
    raw_entities = call.data.get("door_entity")
    raw_devices  = call.data.get("door_device_id")

    entity_ids: list[str] = []
    if raw_entities:
        entity_ids = (
            [raw_entities] if isinstance(raw_entities, str)
            else list(raw_entities)
        )
    device_ids: list[str] = (
        _normalize_device_ids(raw_devices) if raw_devices else []
    )

    seen: set[tuple[str, int]] = set()
    doors_by_entry: dict[str, list[int]] = {}
    invalid_entities: list[str] = []
    invalid_devices:  list[str] = []

    for eid in entity_ids:
        entry_id, door_id = _get_door_id_from_entity(hass, eid)
        if entry_id is None or door_id is None:
            invalid_entities.append(eid)
            continue
        key = (entry_id, int(door_id))
        if key in seen:
            continue
        seen.add(key)
        doors_by_entry.setdefault(entry_id, []).append(int(door_id))

    for did in device_ids:
        entry_id, door_id = _get_door_id_from_device(hass, did)
        if entry_id is None or door_id is None:
            invalid_devices.append(did)
            continue
        key = (entry_id, int(door_id))
        if key in seen:
            continue
        seen.add(key)
        doors_by_entry.setdefault(entry_id, []).append(int(door_id))

    return doors_by_entry, invalid_entities, invalid_devices


def _find_doors_with_code_in_entry(
    hass: HomeAssistant,
    entry_id: str,
    *,
    code: Optional[str] = None,
    code_name: Optional[str] = None,
) -> list[int]:
    """Return the door_ids of all temp_code sensors under entry_id whose
    active_codes contains the given code (PIN value) or code_name.

    Used to broadcast delete/update events to every door's sensor when a
    Hartmann user spans multiple APGs (the post-0.2.5 multi-door model).
    """
    affected: list[int] = []
    seen: set[int] = set()
    ent_reg = er.async_get(hass)

    for entity_id in hass.states.async_entity_ids("sensor"):
        if not entity_id.endswith("_temp_code"):
            continue
        ent_entry = ent_reg.async_get(entity_id)
        if not ent_entry or ent_entry.config_entry_id != entry_id:
            continue
        st = hass.states.get(entity_id)
        if not st:
            continue
        attrs = st.attributes or {}
        door_id = attrs.get("door_id")
        if door_id is None:
            continue
        try:
            did = int(door_id)
        except (TypeError, ValueError):
            continue
        if did in seen:
            continue
        for c in attrs.get("active_codes", []) or []:
            if code is not None and c.get("code") == code:
                affected.append(did)
                seen.add(did)
                break
            if code_name is not None and c.get("code_name") == code_name:
                affected.append(did)
                seen.add(did)
                break
    return affected


def _broadcast_delete(
    hass: HomeAssistant, entry_id: str, code: str, door_ids: list[int]
) -> None:
    """Dispatch a delete event to each given door's temp_code sensor."""
    for did in door_ids:
        async_dispatcher_send(
            hass,
            f"{DISPATCH_TEMP_CODE}_{entry_id}",
            {"action": "delete", "door_id": did, "code": code},
        )


def _broadcast_update(
    hass: HomeAssistant,
    entry_id: str,
    code_name: str,
    door_ids: list[int],
    *,
    end_time: Optional[str] = None,
    start_time: Optional[str] = None,
) -> None:
    """Dispatch an update event to each given door's temp_code sensor."""
    for did in door_ids:
        async_dispatcher_send(
            hass,
            f"{DISPATCH_TEMP_CODE}_{entry_id}",
            {
                "action": "update",
                "door_id": did,
                "code_name": code_name,
                "end_time": end_time,
                "start_time": start_time,
                "timestamp": datetime.now().isoformat(),
            },
        )


async def async_setup_services(hass: HomeAssistant) -> None:
    """Set up Hartmann Control Temp Code services."""
    
    async def handle_create_temp_code(call: ServiceCall) -> dict[str, Any]:
        """Handle the create_temp_code service call.

        With multiple doors selected, this creates **one** Hartmann user with
        a single PIN credential and assigns that user to each door's APG.
        Hartmann's PIN-uniqueness rule rejects duplicate PINs across separate
        users, so the older "one user per door" model could only ever succeed
        for the first door of a multi-door bulk request.
        """
        from . import api
        from .const import DEFAULT_PIN_DIGITS
        
        device_ids = _normalize_device_ids(call.data["door_device_id"])
        code_name = call.data["code_name"]
        random_code = call.data.get("random_code", True)
        manual_code = call.data.get("manual_code")
        start_time = call.data.get("start_time")
        end_time = call.data.get("end_time")
        
        # Generate or use manual code (same code for all doors)
        # Get code_digits from first valid device's config
        code_digits = None
        for device_id in device_ids:
            entry_id, _ = _get_door_id_from_device(hass, device_id)
            if entry_id:
                config_entry = hass.config_entries.async_get_entry(entry_id)
                if config_entry:
                    from .const import DEFAULT_PIN_DIGITS
                    code_digits = call.data.get("code_digits", config_entry.options.get("pin_digits", DEFAULT_PIN_DIGITS))
                    break
        
        if code_digits is None:
            from .const import DEFAULT_PIN_DIGITS
            code_digits = call.data.get("code_digits", DEFAULT_PIN_DIGITS)
        
        if random_code:
            pin_code = generate_random_code(code_digits)
        else:
            if not manual_code:
                _LOGGER.error("Manual code required when random_code is False")
                return {"success": False, "error": "Manual code required"}
            pin_code = manual_code.strip()
            if not pin_code.isdigit():
                _LOGGER.error("Manual code must be numeric")
                return {"success": False, "error": "Code must be numeric"}

        # Group doors by entry_id (in case the user has multiple Hartmann
        # servers configured, each entry needs its own user). We preserve
        # input order for stable result reporting.
        groups: dict[str, dict[str, Any]] = {}
        device_failures: list[dict[str, Any]] = []
        ordered_devices: list[tuple[str, int, str]] = []  # (device_id, door_id, entry_id)

        for device_id in device_ids:
            entry_id, door_id = _get_door_id_from_device(hass, device_id)
            if entry_id is None or door_id is None:
                _LOGGER.error("Could not determine door from device %s", device_id)
                device_failures.append({"device_id": device_id, "success": False, "error": "Invalid door"})
                continue

            if entry_id not in hass.data.get(DOMAIN, {}):
                _LOGGER.error("Entry %s not found in domain data", entry_id)
                device_failures.append({"device_id": device_id, "success": False, "error": "Integration not configured"})
                continue

            grp = groups.setdefault(entry_id, {"door_ids": [], "device_by_door": {}})
            if door_id not in grp["door_ids"]:
                grp["door_ids"].append(door_id)
                grp["device_by_door"][door_id] = device_id
            ordered_devices.append((device_id, door_id, entry_id))

        results: list[dict[str, Any]] = list(device_failures)
        all_success = len(device_failures) == 0
        primary_user_id: Optional[int] = None

        for entry_id, grp in groups.items():
            entry_door_ids: list[int] = grp["door_ids"]
            device_by_door: dict[int, str] = grp["device_by_door"]

            try:
                result = await api.create_temp_code_user(
                    hass=hass,
                    entry_id=entry_id,
                    door_ids=entry_door_ids,
                    code_name=code_name,
                    pin_code=pin_code,
                    start_time=start_time,
                    end_time=end_time,
                )
            except Exception as e:
                _LOGGER.exception(
                    "Error creating temp code for entry %s (doors=%s): %s",
                    entry_id, entry_door_ids, e
                )
                all_success = False
                for did in entry_door_ids:
                    results.append({
                        "device_id": device_by_door[did],
                        "door_id": did,
                        "success": False,
                        "error": str(e),
                    })
                continue

            if not result.get("success"):
                err = result.get("error", "Unknown error")
                _LOGGER.error(
                    "Failed to create temp code for entry %s (doors=%s): %s",
                    entry_id, entry_door_ids, err
                )
                all_success = False
                for did in entry_door_ids:
                    results.append({
                        "device_id": device_by_door[did],
                        "door_id": did,
                        "success": False,
                        "error": err,
                    })
                continue

            user_id = result.get("user_id")
            if primary_user_id is None:
                primary_user_id = user_id

            per_door = result.get("doors") or [{"door_id": d, "success": True} for d in entry_door_ids]

            # Dispatch a create event PER successful door so each door's
            # temp_code sensor picks up the new entry and schedules its own
            # auto-expiration.
            for door_result in per_door:
                did = int(door_result.get("door_id"))
                ok = bool(door_result.get("success"))
                device_id = device_by_door.get(did, "")

                if ok:
                    _LOGGER.info(
                        "Created temp code '%s' for door %d (user %d): %s (valid: %s to %s)",
                        code_name, did, user_id, pin_code, start_time or "now", end_time or "forever"
                    )

                    async_dispatcher_send(
                        hass,
                        f"{DISPATCH_TEMP_CODE}_{entry_id}",
                        {
                            "action": "create",
                            "door_id": did,
                            "code_name": code_name,
                            "code": pin_code,
                            "user_id": user_id,
                            "start_time": start_time,
                            "end_time": end_time,
                            "timestamp": datetime.now().isoformat(),
                        }
                    )

                    results.append({
                        "device_id": device_id,
                        "success": True,
                        "door_id": did,
                        "user_id": user_id,
                    })
                else:
                    err = door_result.get("error", "Unknown error")
                    _LOGGER.warning(
                        "Temp code created (user %d) but door %d APG assignment failed: %s",
                        user_id, did, err
                    )
                    all_success = False
                    results.append({
                        "device_id": device_id,
                        "door_id": did,
                        "success": False,
                        "error": err,
                    })

        # Return single-device format for backwards compatibility if only one device
        if len(device_ids) == 1:
            r0 = results[0] if results else {"success": False, "error": "No results"}
            if r0.get("success"):
                return {
                    "success": True,
                    "code": pin_code,
                    "code_name": code_name,
                    "door_id": r0.get("door_id"),
                    "user_id": r0.get("user_id"),
                    "start_time": start_time,
                    "end_time": end_time,
                }
            else:
                return {"success": False, "error": r0.get("error", "Unknown error")}
        
        # Multi-device response
        return {
            "success": all_success,
            "code": pin_code,
            "code_name": code_name,
            "user_id": primary_user_id,
            "start_time": start_time,
            "end_time": end_time,
            "results": results,
        }
    
    async def handle_delete_temp_code(call: ServiceCall) -> dict[str, Any]:
        """Handle the delete_temp_code service call.

        Deletes the Hartmann user holding the given PIN. Since one user may be
        assigned to multiple doors' APGs (the multi-door model), this dedupes
        per entry and broadcasts the resulting cleanup to every sensor under
        that entry that was tracking the same PIN.
        """
        from . import api

        device_ids = _normalize_device_ids(call.data["door_device_id"])
        code = call.data["code"]

        # Group device_ids by entry_id; one delete call per entry suffices.
        entry_to_devices: dict[str, list[tuple[str, int]]] = {}
        device_failures: list[dict[str, Any]] = []
        for device_id in device_ids:
            entry_id, door_id = _get_door_id_from_device(hass, device_id)
            if entry_id is None or door_id is None:
                _LOGGER.error("Could not determine door from device %s", device_id)
                device_failures.append({"device_id": device_id, "success": False, "error": "Invalid door"})
                continue
            if entry_id not in hass.data.get(DOMAIN, {}):
                _LOGGER.error("Entry %s not found in domain data", entry_id)
                device_failures.append({"device_id": device_id, "success": False, "error": "Integration not configured"})
                continue
            entry_to_devices.setdefault(entry_id, []).append((device_id, door_id))

        results: list[dict[str, Any]] = list(device_failures)
        all_success = len(device_failures) == 0

        for entry_id, dev_door_pairs in entry_to_devices.items():
            # Find every door under this entry whose sensor knows about this
            # code, so we can clean them all up after the user is deleted.
            affected_doors = _find_doors_with_code_in_entry(hass, entry_id, code=code)
            primary_door_id = dev_door_pairs[0][1]

            try:
                result = await api.delete_temp_code_user(
                    hass=hass,
                    entry_id=entry_id,
                    door_id=primary_door_id,
                    pin_code=code,
                )

                if result.get("success"):
                    _LOGGER.info(
                        "Deleted temp code (PIN %s) for entry %s; broadcasting to %d door(s)",
                        code, entry_id, len(affected_doors),
                    )
                    _broadcast_delete(hass, entry_id, code, affected_doors)
                    for device_id, door_id in dev_door_pairs:
                        results.append({"device_id": device_id, "door_id": door_id, "success": True})
                else:
                    err = result.get("error", "Unknown error")
                    _LOGGER.error("Failed to delete temp code: %s", err)
                    all_success = False
                    for device_id, door_id in dev_door_pairs:
                        results.append({"device_id": device_id, "door_id": door_id, "success": False, "error": err})
            except Exception as e:
                _LOGGER.exception("Error deleting temp code: %s", e)
                all_success = False
                for device_id, door_id in dev_door_pairs:
                    results.append({"device_id": device_id, "door_id": door_id, "success": False, "error": str(e)})

        # Single device backward compatibility
        if len(device_ids) == 1:
            r0 = results[0] if results else {"success": False, "error": "No results"}
            if r0.get("success"):
                return {"success": True, "code": code, "door_id": r0.get("door_id")}
            else:
                return {"success": False, "error": r0.get("error", "Unknown error")}

        return {"success": all_success, "code": code, "results": results}
    
    async def handle_delete_temp_code_by_name(call: ServiceCall) -> dict[str, Any]:
        """Handle the delete_temp_code_by_name service call - finds code by name from sensor."""
        from . import api

        device_ids = _normalize_device_ids(call.data["door_device_id"])
        code_name = call.data["code_name"]
        force_remove = call.data.get("force_remove", False)

        # Group device_ids by entry. We resolve each entry's PIN-by-name once.
        entry_to_devices: dict[str, list[tuple[str, int]]] = {}
        device_failures: list[dict[str, Any]] = []
        for device_id in device_ids:
            entry_id, door_id = _get_door_id_from_device(hass, device_id)
            if entry_id is None or door_id is None:
                _LOGGER.error("Could not determine door from device %s", device_id)
                device_failures.append({"device_id": device_id, "success": False, "error": "Invalid door"})
                continue
            if entry_id not in hass.data.get(DOMAIN, {}):
                _LOGGER.error("Entry %s not found in domain data", entry_id)
                device_failures.append({"device_id": device_id, "success": False, "error": "Integration not configured"})
                continue
            entry_to_devices.setdefault(entry_id, []).append((device_id, door_id))

        results: list[dict[str, Any]] = list(device_failures)
        all_success = len(device_failures) == 0

        for entry_id, dev_door_pairs in entry_to_devices.items():
            # Look up the PIN for this code_name from any sensor under entry_id
            code: Optional[str] = None
            for state in hass.states.async_all():
                if not state.entity_id.endswith("_temp_code"):
                    continue
                ent = er.async_get(hass).async_get(state.entity_id)
                if not ent or ent.config_entry_id != entry_id:
                    continue
                for code_entry in (state.attributes or {}).get("active_codes", []) or []:
                    if code_entry.get("code_name") == code_name:
                        code = code_entry.get("code")
                        break
                if code:
                    break

            if not code:
                _LOGGER.warning("No code found with name '%s' under entry %s", code_name, entry_id)
                for device_id, door_id in dev_door_pairs:
                    results.append({
                        "device_id": device_id,
                        "door_id": door_id,
                        "success": False,
                        "error": f"No code found with name '{code_name}'",
                    })
                if not force_remove:
                    all_success = False
                continue

            affected_doors = _find_doors_with_code_in_entry(hass, entry_id, code=code)
            primary_door_id = dev_door_pairs[0][1]

            try:
                result = await api.delete_temp_code_user(
                    hass=hass,
                    entry_id=entry_id,
                    door_id=primary_door_id,
                    pin_code=code,
                )

                if result.get("success"):
                    _LOGGER.info(
                        "Deleted temp code '%s' (PIN %s) for entry %s; broadcasting to %d door(s)",
                        code_name, code, entry_id, len(affected_doors),
                    )
                    _broadcast_delete(hass, entry_id, code, affected_doors)
                    for device_id, door_id in dev_door_pairs:
                        results.append({"device_id": device_id, "door_id": door_id, "code": code, "success": True})
                else:
                    error_msg = result.get("error", "Unknown error")
                    _LOGGER.warning("Hartmann deletion failed for '%s': %s", code_name, error_msg)
                    if force_remove:
                        _LOGGER.info("Force removing '%s' from sensors despite Hartmann error", code_name)
                        _broadcast_delete(hass, entry_id, code, affected_doors)
                        for device_id, door_id in dev_door_pairs:
                            results.append({
                                "device_id": device_id,
                                "door_id": door_id,
                                "code": code,
                                "success": True,
                                "warning": f"Removed from sensor but Hartmann failed: {error_msg}",
                            })
                    else:
                        all_success = False
                        for device_id, door_id in dev_door_pairs:
                            results.append({"device_id": device_id, "door_id": door_id, "success": False, "error": error_msg})

            except Exception as e:
                _LOGGER.exception("Error deleting temp code by name: %s", e)
                if force_remove:
                    _LOGGER.info("Force removing '%s' from sensors despite error", code_name)
                    _broadcast_delete(hass, entry_id, code, affected_doors)
                    for device_id, door_id in dev_door_pairs:
                        results.append({
                            "device_id": device_id,
                            "door_id": door_id,
                            "code": code,
                            "success": True,
                            "warning": f"Removed from sensor but error occurred: {e}",
                        })
                else:
                    all_success = False
                    for device_id, door_id in dev_door_pairs:
                        results.append({"device_id": device_id, "door_id": door_id, "success": False, "error": str(e)})

        # Single device backward compatibility
        if len(device_ids) == 1:
            r = results[0] if results else {"success": False, "error": "No results"}
            if r.get("success"):
                return {"success": True, "code": r.get("code"), "code_name": code_name, "door_id": r.get("door_id"), "warning": r.get("warning")}
            else:
                return {"success": False, "error": r.get("error", "Unknown error")}

        return {"success": all_success, "code_name": code_name, "results": results}
    
    async def handle_clear_all_temp_codes(call: ServiceCall) -> dict[str, Any]:
        """Handle the clear_all_temp_codes service call - removes all codes
        from the requested door(s).

        Note: in the multi-door model, deleting a Hartmann user removes them
        from every APG they belong to. Clearing one door's codes therefore
        also clears any sister-doors' sensors that were tracking the same
        PIN. We dedupe PINs per entry to avoid double-deleting the same user.
        """
        from . import api

        device_ids = _normalize_device_ids(call.data["door_device_id"])

        results: list[dict[str, Any]] = []
        total_cleared = 0
        # Track PINs we've already deleted per entry to avoid duplicate API calls
        cleared_pins_by_entry: dict[str, set[str]] = {}

        for device_id in device_ids:
            entry_id, door_id = _get_door_id_from_device(hass, device_id)
            if entry_id is None or door_id is None:
                _LOGGER.error("Could not determine door from device %s", device_id)
                results.append({"device_id": device_id, "success": False, "error": "Invalid door", "cleared": 0})
                continue

            # Find the temp code sensor for this door
            active_codes: list[dict[str, Any]] = []
            for state in hass.states.async_all():
                if not state.entity_id.endswith("_temp_code"):
                    continue
                attrs = state.attributes or {}
                if attrs.get("door_id") != door_id:
                    continue
                active_codes = list(attrs.get("active_codes", []) or [])
                break

            if not active_codes:
                _LOGGER.info("No active codes to clear for door %d", door_id)
                results.append({"device_id": device_id, "door_id": door_id, "success": True, "cleared": 0})
                continue

            cleared_count = 0
            errors: list[str] = []
            entry_cleared_pins = cleared_pins_by_entry.setdefault(entry_id, set())

            for code_entry in active_codes:
                code = code_entry.get("code")
                code_name = code_entry.get("code_name", "Unknown")
                if not code:
                    continue

                if code in entry_cleared_pins:
                    # Already deleted as part of clearing another door — just
                    # let the broadcast (already sent) clean this sensor up.
                    cleared_count += 1
                    continue

                affected_doors = _find_doors_with_code_in_entry(hass, entry_id, code=code)

                try:
                    result = await api.delete_temp_code_user(
                        hass=hass,
                        entry_id=entry_id,
                        door_id=door_id,
                        pin_code=code,
                    )
                    if result.get("success"):
                        _LOGGER.info("Deleted temp code '%s' (PIN %s) from Hartmann", code_name, code)
                    else:
                        _LOGGER.warning("Hartmann deletion failed for '%s': %s", code_name, result.get("error"))
                except Exception as e:
                    _LOGGER.warning("Error deleting '%s' from Hartmann: %s", code_name, e)
                    errors.append(f"{code_name}: {e}")

                # Always broadcast removal across all sensors that knew about
                # this PIN (force-remove style — Hartmann may already be out
                # of sync).
                _broadcast_delete(hass, entry_id, code, affected_doors)
                entry_cleared_pins.add(code)
                cleared_count += 1

            _LOGGER.info("Cleared %d temp codes for door %d", cleared_count, door_id)
            total_cleared += cleared_count

            r: dict[str, Any] = {"device_id": device_id, "door_id": door_id, "success": True, "cleared": cleared_count}
            if errors:
                r["warnings"] = errors
            results.append(r)

        # Single device backward compatibility
        if len(device_ids) == 1:
            r = results[0] if results else {"success": True, "cleared": 0}
            return {"success": True, "cleared": r.get("cleared", 0), "door_id": r.get("door_id"), "warnings": r.get("warnings")}

        return {"success": True, "total_cleared": total_cleared, "results": results}
    
    # ─────────────────────────────────────────────────────────────────────────
    # Update Temp Code Handler
    # ─────────────────────────────────────────────────────────────────────────
    
    async def handle_update_temp_code(call: ServiceCall) -> dict[str, Any]:
        """Handle the update_temp_code service call - update ExpiresOn/StartedOn.

        With one user spanning multiple doors, the Hartmann update needs to
        happen exactly once, but the resulting end_time/start_time change must
        be reflected in every door's sensor so each one can reschedule its
        auto-expiration.
        """
        from . import api

        device_ids = _normalize_device_ids(call.data["door_device_id"])
        code_name = call.data["code_name"]
        new_end_time = call.data.get("end_time")
        new_start_time = call.data.get("start_time")

        if not new_end_time and not new_start_time:
            return {"success": False, "error": "Must provide at least one of start_time or end_time"}

        # Find user_id from any temp_code sensor's active_codes
        user_id: Optional[int] = None
        target_entry_id: Optional[str] = None

        for device_id in device_ids:
            entry_id, _ = _get_door_id_from_device(hass, device_id)
            if entry_id is None:
                continue
            target_entry_id = entry_id

            for entity_id in hass.states.async_entity_ids("sensor"):
                if not entity_id.endswith("_temp_code"):
                    continue
                ent = er.async_get(hass).async_get(entity_id)
                if not ent or ent.config_entry_id != entry_id:
                    continue
                st = hass.states.get(entity_id)
                if not st or not st.attributes.get("active_codes"):
                    continue
                for code_entry in st.attributes["active_codes"]:
                    if code_entry.get("code_name") == code_name:
                        user_id = code_entry.get("user_id")
                        break
                if user_id:
                    break
            if user_id:
                break

        if not user_id or not target_entry_id:
            return {"success": False, "error": f"No active code found with name '{code_name}'"}

        result = await api.update_temp_code_user(
            hass, target_entry_id, user_id,
            end_time=new_end_time,
            start_time=new_start_time,
        )

        if result.get("success"):
            _LOGGER.info("Updated temp code '%s' (user %d)", code_name, user_id)

            # Broadcast update to every door whose sensor knows about this
            # code_name so each one can update stored times and reschedule
            # its auto-expiration.
            affected_doors = _find_doors_with_code_in_entry(
                hass, target_entry_id, code_name=code_name,
            )
            _broadcast_update(
                hass, target_entry_id, code_name, affected_doors,
                end_time=new_end_time, start_time=new_start_time,
            )

            return {
                "success": True,
                "code_name": code_name,
                "user_id": user_id,
                "end_time": new_end_time,
                "start_time": new_start_time,
            }

        return result
    
    # ─────────────────────────────────────────────────────────────────────────
    # Add / Remove Door for Temp Code Handlers
    # ─────────────────────────────────────────────────────────────────────────

    def _find_temp_code_in_entry(
        entry_id: str,
        *,
        code: Optional[str] = None,
        code_name: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Look up a temp code entry from any sensor under entry_id.

        Returns the first matching active_codes dict (with code, code_name,
        user_id, start_time, end_time) or None if no match is found.
        """
        ent_reg = er.async_get(hass)
        for entity_id in hass.states.async_entity_ids("sensor"):
            if not entity_id.endswith("_temp_code"):
                continue
            ent = ent_reg.async_get(entity_id)
            if not ent or ent.config_entry_id != entry_id:
                continue
            st = hass.states.get(entity_id)
            if not st:
                continue
            for c in (st.attributes or {}).get("active_codes", []) or []:
                if code is not None and c.get("code") == code:
                    return dict(c)
                if code_name is not None and c.get("code_name") == code_name:
                    return dict(c)
        return None

    async def handle_add_door_to_temp_code(call: ServiceCall) -> dict[str, Any]:
        """Handle the add_door_to_temp_code service call.

        Adds an existing temp code (identified by `code` or `code_name`) to
        one or more additional doors by assigning the underlying Hartmann
        user to each door's APG. Each door's temp_code sensor receives a
        create event so it picks up the new entry.
        """
        from . import api

        device_ids = _normalize_device_ids(call.data["door_device_id"])
        code = call.data.get("code")
        code_name = call.data.get("code_name")

        # Group device_ids by entry_id; the temp code is scoped to one entry.
        entry_to_devices: dict[str, list[tuple[str, int]]] = {}
        device_failures: list[dict[str, Any]] = []
        for device_id in device_ids:
            entry_id, door_id = _get_door_id_from_device(hass, device_id)
            if entry_id is None or door_id is None:
                _LOGGER.error("Could not determine door from device %s", device_id)
                device_failures.append({"device_id": device_id, "success": False, "error": "Invalid door"})
                continue
            if entry_id not in hass.data.get(DOMAIN, {}):
                _LOGGER.error("Entry %s not found in domain data", entry_id)
                device_failures.append({"device_id": device_id, "success": False, "error": "Integration not configured"})
                continue
            entry_to_devices.setdefault(entry_id, []).append((device_id, door_id))

        results: list[dict[str, Any]] = list(device_failures)
        all_success = len(device_failures) == 0

        for entry_id, dev_door_pairs in entry_to_devices.items():
            existing = _find_temp_code_in_entry(entry_id, code=code, code_name=code_name)
            if not existing:
                identifier = code or code_name
                err = f"No active temp code found matching '{identifier}' under this integration"
                _LOGGER.warning(err)
                all_success = False
                for device_id, door_id in dev_door_pairs:
                    results.append({"device_id": device_id, "door_id": door_id, "success": False, "error": err})
                continue

            existing_code = existing.get("code")
            existing_name = existing.get("code_name")
            user_id = existing.get("user_id")
            start_time = existing.get("start_time")
            end_time = existing.get("end_time")

            if not user_id:
                err = "Existing temp code has no user_id (created on a pre-0.2.5 version?)"
                _LOGGER.error(err)
                all_success = False
                for device_id, door_id in dev_door_pairs:
                    results.append({"device_id": device_id, "door_id": door_id, "success": False, "error": err})
                continue

            for device_id, door_id in dev_door_pairs:
                # Skip if this door's sensor already has the code
                already_has = False
                ent_reg = er.async_get(hass)
                for entity_id in hass.states.async_entity_ids("sensor"):
                    if not entity_id.endswith("_temp_code"):
                        continue
                    ent = ent_reg.async_get(entity_id)
                    if not ent or ent.config_entry_id != entry_id:
                        continue
                    st = hass.states.get(entity_id)
                    if not st or st.attributes.get("door_id") != door_id:
                        continue
                    for c in (st.attributes or {}).get("active_codes", []) or []:
                        if c.get("code") == existing_code:
                            already_has = True
                            break
                    break

                if already_has:
                    _LOGGER.info(
                        "Door %d already has temp code '%s' — skipping",
                        door_id, existing_name,
                    )
                    results.append({
                        "device_id": device_id,
                        "door_id": door_id,
                        "success": True,
                        "note": "Door already had this code",
                    })
                    continue

                try:
                    api_result = await api.add_user_to_door_apg(
                        hass=hass,
                        entry_id=entry_id,
                        user_id=int(user_id),
                        door_id=door_id,
                    )
                except Exception as e:
                    _LOGGER.exception("Error adding door %d to temp code: %s", door_id, e)
                    all_success = False
                    results.append({"device_id": device_id, "door_id": door_id, "success": False, "error": str(e)})
                    continue

                if api_result.get("success"):
                    _LOGGER.info(
                        "Added door %d to temp code '%s' (user %d)",
                        door_id, existing_name, user_id,
                    )
                    # Dispatch a create event to JUST this door's sensor so
                    # it adds the entry to its active_codes list.
                    async_dispatcher_send(
                        hass,
                        f"{DISPATCH_TEMP_CODE}_{entry_id}",
                        {
                            "action": "create",
                            "door_id": door_id,
                            "code_name": existing_name,
                            "code": existing_code,
                            "user_id": user_id,
                            "start_time": start_time,
                            "end_time": end_time,
                            "timestamp": datetime.now().isoformat(),
                        },
                    )
                    results.append({"device_id": device_id, "door_id": door_id, "success": True})
                else:
                    err = api_result.get("error", "Unknown error")
                    _LOGGER.error("Failed to add door %d to temp code: %s", door_id, err)
                    all_success = False
                    results.append({"device_id": device_id, "door_id": door_id, "success": False, "error": err})

        if len(device_ids) == 1:
            r0 = results[0] if results else {"success": False, "error": "No results"}
            if r0.get("success"):
                return {"success": True, "code_name": code_name, "code": code, "door_id": r0.get("door_id")}
            return {"success": False, "error": r0.get("error", "Unknown error")}

        return {
            "success": all_success,
            "code": code,
            "code_name": code_name,
            "results": results,
        }

    async def handle_remove_door_from_temp_code(call: ServiceCall) -> dict[str, Any]:
        """Handle the remove_door_from_temp_code service call.

        Removes a temp code's access from one or more specific doors by
        DELETE-ing the underlying Hartmann user from each door's APG. The
        Hartmann user record itself is left alone — even if every door is
        removed, the user persists and can be re-added or deleted manually.
        Each affected door's temp_code sensor receives a delete event for
        only its own entry; sister doors keep their entries.
        """
        from . import api

        device_ids = _normalize_device_ids(call.data["door_device_id"])
        code = call.data.get("code")
        code_name = call.data.get("code_name")

        entry_to_devices: dict[str, list[tuple[str, int]]] = {}
        device_failures: list[dict[str, Any]] = []
        for device_id in device_ids:
            entry_id, door_id = _get_door_id_from_device(hass, device_id)
            if entry_id is None or door_id is None:
                _LOGGER.error("Could not determine door from device %s", device_id)
                device_failures.append({"device_id": device_id, "success": False, "error": "Invalid door"})
                continue
            if entry_id not in hass.data.get(DOMAIN, {}):
                _LOGGER.error("Entry %s not found in domain data", entry_id)
                device_failures.append({"device_id": device_id, "success": False, "error": "Integration not configured"})
                continue
            entry_to_devices.setdefault(entry_id, []).append((device_id, door_id))

        results: list[dict[str, Any]] = list(device_failures)
        all_success = len(device_failures) == 0

        for entry_id, dev_door_pairs in entry_to_devices.items():
            existing = _find_temp_code_in_entry(entry_id, code=code, code_name=code_name)
            if not existing:
                identifier = code or code_name
                err = f"No active temp code found matching '{identifier}' under this integration"
                _LOGGER.warning(err)
                all_success = False
                for device_id, door_id in dev_door_pairs:
                    results.append({"device_id": device_id, "door_id": door_id, "success": False, "error": err})
                continue

            existing_code = existing.get("code")
            existing_name = existing.get("code_name")
            user_id = existing.get("user_id")

            if not user_id:
                err = "Existing temp code has no user_id"
                _LOGGER.error(err)
                all_success = False
                for device_id, door_id in dev_door_pairs:
                    results.append({"device_id": device_id, "door_id": door_id, "success": False, "error": err})
                continue

            for device_id, door_id in dev_door_pairs:
                try:
                    api_result = await api.remove_user_from_door_apg(
                        hass=hass,
                        entry_id=entry_id,
                        user_id=int(user_id),
                        door_id=door_id,
                    )
                except Exception as e:
                    _LOGGER.exception("Error removing door %d from temp code: %s", door_id, e)
                    all_success = False
                    results.append({"device_id": device_id, "door_id": door_id, "success": False, "error": str(e)})
                    continue

                if api_result.get("success"):
                    _LOGGER.info(
                        "Removed door %d from temp code '%s' (user %d)",
                        door_id, existing_name, user_id,
                    )
                    # Dispatch delete to JUST this door's sensor — other
                    # doors keep their entries.
                    async_dispatcher_send(
                        hass,
                        f"{DISPATCH_TEMP_CODE}_{entry_id}",
                        {
                            "action": "delete",
                            "door_id": door_id,
                            "code": existing_code,
                        },
                    )
                    results.append({"device_id": device_id, "door_id": door_id, "success": True})
                else:
                    err = api_result.get("error", "Unknown error")
                    _LOGGER.error("Failed to remove door %d from temp code: %s", door_id, err)
                    all_success = False
                    results.append({"device_id": device_id, "door_id": door_id, "success": False, "error": err})

        if len(device_ids) == 1:
            r0 = results[0] if results else {"success": False, "error": "No results"}
            if r0.get("success"):
                return {"success": True, "code_name": code_name, "code": code, "door_id": r0.get("door_id")}
            return {"success": False, "error": r0.get("error", "Unknown error")}

        return {
            "success": all_success,
            "code": code,
            "code_name": code_name,
            "results": results,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Update Panels Handler
    # ─────────────────────────────────────────────────────────────────────────
    
    async def handle_update_panels(call: ServiceCall) -> dict[str, Any]:
        """Handle the update_panels service call - push config to all panels."""
        from . import api
        
        # Use the first available entry_id
        entry_id = next(iter(
            eid for eid in hass.data.get(DOMAIN, {})
            if isinstance(hass.data[DOMAIN][eid], dict) and "base_url" in hass.data[DOMAIN][eid]
        ), None)
        
        if not entry_id:
            return {"success": False, "error": "No Protector.Net integration found"}
        
        result = await api.update_panels(hass, entry_id)
        return result
    
    # ─────────────────────────────────────────────────────────────────────────
    # OTR Schedule Handlers
    # ─────────────────────────────────────────────────────────────────────────
    
    async def handle_create_otr_schedule(call: ServiceCall) -> dict[str, Any]:
        """Handle the create_otr_schedule service call."""
        from . import api
        
        device_ids = _normalize_device_ids(call.data["door_device_id"])
        start_time = call.data["start_time"]
        stop_time = call.data["stop_time"]
        mode = call.data.get("mode", "Unlock")
        name = call.data.get("name")
        description = call.data.get("description")
        
        # Group doors by entry_id - OTR creates one schedule with multiple doors
        doors_by_entry: dict[str, list[int]] = {}
        
        for device_id in device_ids:
            entry_id, door_id = _get_door_id_from_device(hass, device_id)
            if entry_id is None or door_id is None:
                _LOGGER.warning("Could not determine door from device %s, skipping", device_id)
                continue
            
            if entry_id not in doors_by_entry:
                doors_by_entry[entry_id] = []
            doors_by_entry[entry_id].append(door_id)
        
        if not doors_by_entry:
            return {"success": False, "error": "No valid doors found"}
        
        # Create one schedule per Hartmann instance
        results = []
        all_success = True
        
        for entry_id, door_ids in doors_by_entry.items():
            if entry_id not in hass.data.get(DOMAIN, {}):
                _LOGGER.error("Entry %s not found in domain data", entry_id)
                results.append({"entry_id": entry_id, "success": False, "error": "Integration not configured"})
                all_success = False
                continue
            
            try:
                result = await api.create_one_time_run(
                    hass=hass,
                    entry_id=entry_id,
                    door_ids=door_ids,
                    start_time=start_time,
                    stop_time=stop_time,
                    mode=mode,
                    name=name,
                    description=description,
                )
                
                if result.get("success"):
                    _LOGGER.info("Created OTR schedule for doors %s: %s to %s (%s)", 
                                door_ids, start_time, stop_time, mode)
                    results.append(result)
                    # Signal OTR sensors to refresh immediately (short delay for Hartmann to process)
                    import asyncio
                    await asyncio.sleep(1)
                    async_dispatcher_send(
                        hass,
                        f"{DISPATCH_OTR}_{entry_id}",
                    )
                else:
                    _LOGGER.error("Failed to create OTR schedule: %s", result.get("error"))
                    results.append(result)
                    all_success = False
                
            except Exception as e:
                _LOGGER.exception("Error creating OTR schedule: %s", e)
                results.append({"entry_id": entry_id, "door_ids": door_ids, "success": False, "error": str(e)})
                all_success = False
        
        # Single entry/result backward compatibility
        if len(results) == 1:
            return results[0]
        
        return {"success": all_success, "results": results}
    
    async def handle_delete_otr_schedule(call: ServiceCall) -> dict[str, Any]:
        """Handle the delete_otr_schedule service call.
        
        If schedule_id is provided, deletes that specific schedule.
        If only door_device_id is provided, deletes ALL OTR schedules for that door.
        """
        from . import api
        
        schedule_id = call.data.get("schedule_id")
        device_id = call.data.get("door_device_id")
        
        # Get entry_id and door_id from device
        entry_id = None
        door_id = None
        if device_id:
            entry_id, door_id = _get_door_id_from_device(hass, device_id)
        
        if entry_id is None:
            # Try to find any available entry
            domain_data = hass.data.get(DOMAIN, {})
            for eid in domain_data:
                if isinstance(domain_data[eid], dict) and "base_url" in domain_data[eid]:
                    entry_id = eid
                    break
        
        if entry_id is None:
            _LOGGER.error("Could not determine integration entry")
            return {"success": False, "error": "Integration not configured"}
        
        # Collect schedule IDs to delete
        ids_to_delete = []
        
        if schedule_id is not None and schedule_id > 0:
            # Explicit ID given — delete just that one
            ids_to_delete = [schedule_id]
        elif door_id is not None:
            # No valid schedule_id — find all schedules for this door and delete them
            try:
                schedules = await api.get_one_time_runs(hass, entry_id, door_id=door_id)
                ids_to_delete = [s["id"] for s in schedules if s.get("id") is not None and s["id"] > 0]
                _LOGGER.info("Found %d schedules to delete for door %d: %s", len(ids_to_delete), door_id, ids_to_delete)
            except Exception as e:
                _LOGGER.exception("Error finding schedules for door %d: %s", door_id, e)
                return {"success": False, "error": f"Could not look up schedules: {e}"}
        else:
            return {"success": False, "error": "Provide either a schedule_id or a door_device_id"}
        
        if not ids_to_delete:
            return {"success": True, "message": "No schedules found to delete", "deleted": 0}
        
        # Delete each schedule
        deleted = 0
        errors = []
        for sid in ids_to_delete:
            try:
                result = await api.delete_one_time_run(hass=hass, entry_id=entry_id, schedule_id=sid)
                if result.get("success"):
                    deleted += 1
                    _LOGGER.info("Deleted OTR schedule ID %d", sid)
                else:
                    errors.append(f"ID {sid}: {result.get('error')}")
            except Exception as e:
                errors.append(f"ID {sid}: {e}")
        
        # Signal OTR sensors to refresh
        if deleted > 0:
            async_dispatcher_send(hass, f"{DISPATCH_OTR}_{entry_id}")
        
        if errors:
            _LOGGER.error("Some schedule deletes failed: %s", errors)
            return {"success": deleted > 0, "deleted": deleted, "errors": errors}
        
        return {"success": True, "deleted": deleted}
    
    async def handle_get_otr_schedules(call: ServiceCall) -> dict[str, Any]:
        """Handle the get_otr_schedules service call."""
        from . import api
        
        device_id = call.data.get("door_device_id")
        
        # Get entry_id and optionally door_id
        entry_id = None
        door_id = None
        if device_id:
            entry_id, door_id = _get_door_id_from_device(hass, device_id)
        
        if entry_id is None:
            # Try to find any available entry
            domain_data = hass.data.get(DOMAIN, {})
            for eid in domain_data:
                if isinstance(domain_data[eid], dict) and "base_url" in domain_data[eid]:
                    entry_id = eid
                    break
        
        if entry_id is None:
            _LOGGER.error("Could not determine integration entry")
            return {"success": False, "error": "Integration not configured", "schedules": []}
        
        try:
            schedules = await api.get_one_time_runs(
                hass=hass,
                entry_id=entry_id,
                door_id=door_id,
            )
            
            _LOGGER.info("Retrieved OTR schedules", len(schedules))
            return {"success": True, "schedules": schedules}
            
        except Exception as e:
            _LOGGER.exception("Error getting OTR schedules: %s", e)
            return {"success": False, "error": str(e), "schedules": []}
    
    # ─────────────────────────────────────────────────────────────────────────
    # Override Door / Resume Door Services
    # ─────────────────────────────────────────────────────────────────────────
    
    async def handle_override_door(call: ServiceCall) -> dict[str, Any]:
        """Handle the override_door service call - apply override to doors."""
        from . import api
        from homeassistant.util import dt as dt_util
        import math
        
        mode = call.data.get("mode", "Unlock")
        override_type = call.data.get("override_type", "until_resumed")
        minutes = call.data.get("minutes")
        until_raw = call.data.get("until")
        
        # --- 'until' datetime support ---
        # If 'until' is provided, auto-set override_type to for_time and
        # compute minutes from (until - now).
        if until_raw:
            try:
                until_dt = datetime.fromisoformat(str(until_raw))
                if until_dt.tzinfo is None:
                    until_dt = dt_util.as_local(until_dt)
                now = dt_util.now()
                delta_seconds = (until_dt - now).total_seconds()
                if delta_seconds <= 0:
                    return {"success": False, "error": f"'until' datetime {until_raw} is in the past"}
                minutes = max(1, math.ceil(delta_seconds / 60))
                override_type = "for_time"
                _LOGGER.info("override_door: 'until' %s -> computed %d minutes", until_raw, minutes)
            except (ValueError, TypeError) as e:
                return {"success": False, "error": f"Invalid 'until' datetime: {until_raw} ({e})"}
        
        # Map override_type to API token
        type_map = {
            "until_resumed": "Resume",
            "for_time": "Time",
            "until_schedule": "Schedule",
        }
        type_token = type_map.get(override_type, "Resume")
        
        # Minutes only used for "for_time" type
        minutes_arg = minutes if type_token == "Time" else None
        
        # Validate minutes required for timed override
        if type_token == "Time" and not minutes_arg:
            return {"success": False, "error": "minutes required for 'for_time' override type"}
        
        # Resolve targets from door_entity and/or door_device_id (legacy)
        doors_by_entry, invalid_entities, invalid_devices = _resolve_door_targets(hass, call)
        
        if not doors_by_entry:
            return {
                "success": False,
                "error": "No valid doors found",
                "invalid_entities": invalid_entities,
                "invalid_devices":  invalid_devices,
            }
        
        all_success = True
        results = []
        
        for entry_id, door_ids in doors_by_entry.items():
            try:
                ok = await api.apply_override(
                    hass,
                    entry_id,
                    door_ids,
                    override_type=type_token,
                    mode=mode,
                    minutes=minutes_arg,
                )
                
                if ok:
                    _LOGGER.info("Applied %s override (%s) to doors %s", mode, override_type, door_ids)
                    results.append({"entry_id": entry_id, "door_ids": door_ids, "success": True})
                    
                    # Sync UI state so select entities reflect what was just set
                    ui_type_map = {
                        "until_resumed": "Until Resumed",
                        "for_time": "For Specified Time",
                        "until_schedule": "Until Next Schedule",
                    }
                    ui_type_label = ui_type_map.get(override_type, "For Specified Time")
                    
                    for did in door_ids:
                        ui = hass.data.get(DOMAIN, {}).get(entry_id, {}).get(UI_STATE, {}).get(did)
                        if ui is not None:
                            ui["type"] = ui_type_label
                            ui["mode_selected"] = mode
                            ui["active"] = True
                else:
                    _LOGGER.error("Failed to apply override to doors %s", door_ids)
                    results.append({"entry_id": entry_id, "door_ids": door_ids, "success": False, "error": "Override failed"})
                    all_success = False
                    
            except Exception as e:
                _LOGGER.exception("Error applying override: %s", e)
                results.append({"entry_id": entry_id, "door_ids": door_ids, "success": False, "error": str(e)})
                all_success = False
        
        # Single-target backward compatibility (preserve flat shape when one door)
        if (len(doors_by_entry) == 1 and
            len(next(iter(doors_by_entry.values()))) == 1):
            entry_id = next(iter(doors_by_entry))
            door_id = doors_by_entry[entry_id][0]
            return {
                "success": all_success,
                "door_id": door_id,
                "mode": mode,
                "override_type": override_type,
                "minutes": minutes_arg,
            }
        
        out: dict[str, Any] = {
            "success": all_success,
            "mode": mode,
            "override_type": override_type,
            "minutes": minutes_arg,
            "results": results,
        }
        if invalid_entities:
            out["invalid_entities"] = invalid_entities
        if invalid_devices:
            out["invalid_devices"] = invalid_devices
        return out
    
    async def handle_resume_door(call: ServiceCall) -> dict[str, Any]:
        """Handle the resume_door service call - resume normal schedule."""
        from . import api
        
        # Resolve targets from door_entity and/or door_device_id (legacy)
        doors_by_entry, invalid_entities, invalid_devices = _resolve_door_targets(hass, call)
        
        if not doors_by_entry:
            return {
                "success": False,
                "error": "No valid doors found",
                "invalid_entities": invalid_entities,
                "invalid_devices":  invalid_devices,
            }
        
        all_success = True
        results = []
        
        for entry_id, door_ids in doors_by_entry.items():
            try:
                ok = await api.resume_schedule(hass, entry_id, door_ids)
                
                if ok:
                    _LOGGER.info("Resumed schedule for doors %s", door_ids)
                    results.append({"entry_id": entry_id, "door_ids": door_ids, "success": True})
                    
                    # Sync UI state
                    for did in door_ids:
                        ui = hass.data.get(DOMAIN, {}).get(entry_id, {}).get(UI_STATE, {}).get(did)
                        if ui is not None:
                            ui["active"] = False
                            ui["mode_selected"] = "None"
                else:
                    _LOGGER.error("Failed to resume schedule for doors %s", door_ids)
                    results.append({"entry_id": entry_id, "door_ids": door_ids, "success": False, "error": "Resume failed"})
                    all_success = False
                    
            except Exception as e:
                _LOGGER.exception("Error resuming schedule: %s", e)
                results.append({"entry_id": entry_id, "door_ids": door_ids, "success": False, "error": str(e)})
                all_success = False
        
        # Single-target backward compatibility
        if (len(doors_by_entry) == 1 and
            len(next(iter(doors_by_entry.values()))) == 1):
            entry_id = next(iter(doors_by_entry))
            door_id = doors_by_entry[entry_id][0]
            return {"success": all_success, "door_id": door_id}
        
        out: dict[str, Any] = {"success": all_success, "results": results}
        if invalid_entities:
            out["invalid_entities"] = invalid_entities
        if invalid_devices:
            out["invalid_devices"] = invalid_devices
        return out
    
    async def handle_set_door_schedule_mode(call: ServiceCall) -> dict[str, Any]:
        """Handle the set_door_schedule_mode service call.

        Updates the HA-managed DoorTimeZone for the given door(s). If a door
        is currently Active (using the HA TZ), an Update Panels is fired so
        the change reaches the panel hardware. If a door is only Provisioned
        (not yet Active), the HA TZ is updated but no panel push happens —
        the new mode takes effect when the user activates the door.

        Refuses doors that aren't in the managed set; in that case the user
        should add them via integration reconfigure first.
        """
        from . import api, managed_schedules

        # Entity-only — this service was introduced fresh in v0.2.5.
        raw_entities = call.data["door_entity"]
        entity_ids = (
            [raw_entities] if isinstance(raw_entities, str)
            else list(raw_entities)
        )
        mode = call.data["mode"]

        # Group doors by entry_id so we can fire Update Panels at most once
        # per entry across the whole batch.
        per_entry: dict[str, list[int]] = {}
        unmanaged: list[int] = []
        invalid: list[str] = []
        seen: set[tuple[str, int]] = set()

        for eid in entity_ids:
            entry_id, door_id = _get_door_id_from_entity(hass, eid)
            if entry_id is None or door_id is None:
                invalid.append(eid)
                continue
            key = (entry_id, int(door_id))
            if key in seen:
                continue
            seen.add(key)
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is None or not managed_schedules.is_managed(entry, door_id):
                unmanaged.append(door_id)
                continue
            per_entry.setdefault(entry_id, []).append(int(door_id))

        if not per_entry:
            return {
                "success": False,
                "error": (
                    "No managed doors selected. Add the door(s) under "
                    "integration options -> Door Time Zones first."
                ),
                "unmanaged_door_ids": unmanaged,
                "invalid_entities":   invalid,
            }

        results: list[dict[str, Any]] = []
        all_success = True

        for entry_id, door_ids in per_entry.items():
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is None:
                continue

            any_active_changed = False
            updated_managed = dict(entry.options.get("managed_doors") or {})

            for door_id in door_ids:
                res = await managed_schedules.set_mode(hass, entry, door_id, mode)
                if res.get("success"):
                    if res.get("changed"):
                        # Mirror the mode into our local copy of options.
                        info = dict(updated_managed.get(str(door_id)) or {})
                        info["current_mode"] = mode
                        updated_managed[str(door_id)] = info
                        if res.get("active"):
                            any_active_changed = True
                    results.append({
                        "entry_id": entry_id,
                        "door_id":  door_id,
                        "mode":     mode,
                        "success":  True,
                        "changed":  bool(res.get("changed")),
                        "active":   bool(res.get("active")),
                    })
                else:
                    all_success = False
                    results.append({
                        "entry_id": entry_id,
                        "door_id":  door_id,
                        "mode":     mode,
                        "success":  False,
                        "error":    res.get("error"),
                    })

            # Persist the updated current_mode values for this entry.
            new_options = {**entry.options, "managed_doors": updated_managed}
            hass.config_entries.async_update_entry(entry, options=new_options)

            # Request a coalesced Update Panels if any active door's TZ
            # actually changed. Using request_update_panels (a per-entry
            # debouncer) instead of a direct push means that when an
            # automation makes several set_door_schedule_mode calls in quick
            # succession — e.g. a batch lock immediately followed by a single
            # extra door in a second call ~0.5s later — the pushes collapse
            # into ONE UpdateAll that fires after every DoorTimeZone write has
            # committed, instead of two UpdateAlls racing on the panel.
            if any_active_changed:
                try:
                    await api.request_update_panels(hass, entry_id)
                except Exception as e:
                    _LOGGER.warning(
                        "%s: Update Panels after set_door_schedule_mode failed: %s",
                        entry_id, e,
                    )

            # Nudge the Hub "Door Schedules" sensor to refresh now so it
            # reflects the new mode/assignment without waiting for its poll.
            async_dispatcher_send(hass, f"{DISPATCH_DOOR_SCHEDULES}_{entry_id}")

        if unmanaged:
            results.append({"unmanaged_door_ids": unmanaged})
        if invalid:
            results.append({"invalid_entities": invalid})

        return {"success": all_success, "mode": mode, "results": results}

    # Register services with response support
    hass.services.async_register(
        DOMAIN,
        SERVICE_CREATE_TEMP_CODE,
        handle_create_temp_code,
        schema=SERVICE_CREATE_TEMP_CODE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    
    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_TEMP_CODE,
        handle_delete_temp_code,
        schema=SERVICE_DELETE_TEMP_CODE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    
    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_TEMP_CODE_BY_NAME,
        handle_delete_temp_code_by_name,
        schema=SERVICE_DELETE_TEMP_CODE_BY_NAME_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_ALL_TEMP_CODES,
        handle_clear_all_temp_codes,
        schema=SERVICE_CLEAR_ALL_TEMP_CODES_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    
    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_TEMP_CODE,
        handle_update_temp_code,
        schema=SERVICE_UPDATE_TEMP_CODE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_DOOR_TO_TEMP_CODE,
        handle_add_door_to_temp_code,
        schema=SERVICE_DOOR_FOR_TEMP_CODE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REMOVE_DOOR_FROM_TEMP_CODE,
        handle_remove_door_from_temp_code,
        schema=SERVICE_DOOR_FOR_TEMP_CODE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    
    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_PANELS,
        handle_update_panels,
        schema=vol.Schema({}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    
    # OTR Schedule services
    hass.services.async_register(
        DOMAIN,
        SERVICE_CREATE_OTR_SCHEDULE,
        handle_create_otr_schedule,
        schema=SERVICE_CREATE_OTR_SCHEDULE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    
    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_OTR_SCHEDULE,
        handle_delete_otr_schedule,
        schema=SERVICE_DELETE_OTR_SCHEDULE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_OTR_SCHEDULES,
        handle_get_otr_schedules,
        schema=SERVICE_GET_OTR_SCHEDULES_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    
    # Override / Resume door services
    hass.services.async_register(
        DOMAIN,
        SERVICE_OVERRIDE_DOOR,
        handle_override_door,
        schema=SERVICE_OVERRIDE_DOOR_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESUME_DOOR,
        handle_resume_door,
        schema=SERVICE_RESUME_DOOR_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_DOOR_SCHEDULE_MODE,
        handle_set_door_schedule_mode,
        schema=SERVICE_SET_DOOR_SCHEDULE_MODE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    _LOGGER.info("Registered Hartmann Control services (temp codes + OTR schedules + override/resume + managed schedules)")


async def async_unload_services(hass: HomeAssistant) -> None:
    """Unload Hartmann Control services."""
    hass.services.async_remove(DOMAIN, SERVICE_CREATE_TEMP_CODE)
    hass.services.async_remove(DOMAIN, SERVICE_DELETE_TEMP_CODE)
    hass.services.async_remove(DOMAIN, SERVICE_DELETE_TEMP_CODE_BY_NAME)
    hass.services.async_remove(DOMAIN, SERVICE_CLEAR_ALL_TEMP_CODES)
    hass.services.async_remove(DOMAIN, SERVICE_UPDATE_TEMP_CODE)
    hass.services.async_remove(DOMAIN, SERVICE_UPDATE_PANELS)
    hass.services.async_remove(DOMAIN, SERVICE_ADD_DOOR_TO_TEMP_CODE)
    hass.services.async_remove(DOMAIN, SERVICE_REMOVE_DOOR_FROM_TEMP_CODE)
    hass.services.async_remove(DOMAIN, SERVICE_CREATE_OTR_SCHEDULE)
    hass.services.async_remove(DOMAIN, SERVICE_DELETE_OTR_SCHEDULE)
    hass.services.async_remove(DOMAIN, SERVICE_GET_OTR_SCHEDULES)
    hass.services.async_remove(DOMAIN, SERVICE_OVERRIDE_DOOR)
    hass.services.async_remove(DOMAIN, SERVICE_RESUME_DOOR)
    hass.services.async_remove(DOMAIN, SERVICE_SET_DOOR_SCHEDULE_MODE)
    _LOGGER.info("Unregistered Hartmann Control services")

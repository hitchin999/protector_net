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
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DOMAIN, UI_STATE

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
SERVICE_OVERRIDE_DOOR_SCHEMA = vol.Schema(
    {
        vol.Required("door_device_id"): DEVICE_ID_SCHEMA,
        vol.Optional("mode", default="Unlock"): vol.In(OVERRIDE_DOOR_MODES),
        vol.Optional("override_type", default="until_resumed"): vol.In(OVERRIDE_TYPES),
        vol.Optional("minutes"): vol.All(vol.Coerce(int), vol.Range(min=1)),
    }
)

# Schema for resume_door service
SERVICE_RESUME_DOOR_SCHEMA = vol.Schema(
    {
        vol.Required("door_device_id"): DEVICE_ID_SCHEMA,
    }
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


async def async_setup_services(hass: HomeAssistant) -> None:
    """Set up Hartmann Control Temp Code services."""
    
    async def handle_create_temp_code(call: ServiceCall) -> dict[str, Any]:
        """Handle the create_temp_code service call."""
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
        
        # Process all devices
        results = []
        all_success = True
        
        for device_id in device_ids:
            entry_id, door_id = _get_door_id_from_device(hass, device_id)
            if entry_id is None or door_id is None:
                _LOGGER.error("Could not determine door from device %s", device_id)
                results.append({"device_id": device_id, "success": False, "error": "Invalid door"})
                all_success = False
                continue
            
            if entry_id not in hass.data.get(DOMAIN, {}):
                _LOGGER.error("Entry %s not found in domain data", entry_id)
                results.append({"device_id": device_id, "success": False, "error": "Integration not configured"})
                all_success = False
                continue
            
            try:
                result = await api.create_temp_code_user(
                    hass=hass,
                    entry_id=entry_id,
                    door_id=door_id,
                    code_name=code_name,
                    pin_code=pin_code,
                    start_time=start_time,
                    end_time=end_time,
                )
                
                if result.get("success"):
                    _LOGGER.info(
                        "Created temp code '%s' for door %d: %s (valid: %s to %s)",
                        code_name, door_id, pin_code, start_time or "now", end_time or "forever"
                    )
                    
                    async_dispatcher_send(
                        hass,
                        f"{DISPATCH_TEMP_CODE}_{entry_id}",
                        {
                            "action": "create",
                            "door_id": door_id,
                            "code_name": code_name,
                            "code": pin_code,
                            "user_id": result.get("user_id"),
                            "start_time": start_time,
                            "end_time": end_time,
                            "timestamp": datetime.now().isoformat(),
                        }
                    )
                    
                    results.append({
                        "device_id": device_id,
                        "success": True,
                        "door_id": door_id,
                        "user_id": result.get("user_id"),
                    })
                else:
                    _LOGGER.error("Failed to create temp code for door %d: %s", door_id, result.get("error"))
                    results.append({"device_id": device_id, "success": False, "error": result.get("error", "Unknown error")})
                    all_success = False
                    
            except Exception as e:
                _LOGGER.exception("Error creating temp code for device %s: %s", device_id, e)
                results.append({"device_id": device_id, "success": False, "error": str(e)})
                all_success = False
        
        # Return single-device format for backwards compatibility if only one device
        if len(device_ids) == 1:
            if all_success:
                return {
                    "success": True,
                    "code": pin_code,
                    "code_name": code_name,
                    "door_id": results[0].get("door_id"),
                    "user_id": results[0].get("user_id"),
                    "start_time": start_time,
                    "end_time": end_time,
                }
            else:
                return {"success": False, "error": results[0].get("error", "Unknown error")}
        
        # Multi-device response
        return {
            "success": all_success,
            "code": pin_code,
            "code_name": code_name,
            "start_time": start_time,
            "end_time": end_time,
            "results": results,
        }
    
    async def handle_delete_temp_code(call: ServiceCall) -> dict[str, Any]:
        """Handle the delete_temp_code service call."""
        from . import api
        
        device_ids = _normalize_device_ids(call.data["door_device_id"])
        code = call.data["code"]
        
        results = []
        all_success = True
        
        for device_id in device_ids:
            entry_id, door_id = _get_door_id_from_device(hass, device_id)
            if entry_id is None or door_id is None:
                _LOGGER.error("Could not determine door from device %s", device_id)
                results.append({"device_id": device_id, "success": False, "error": "Invalid door"})
                all_success = False
                continue
            
            if entry_id not in hass.data.get(DOMAIN, {}):
                _LOGGER.error("Entry %s not found in domain data", entry_id)
                results.append({"device_id": device_id, "success": False, "error": "Integration not configured"})
                all_success = False
                continue
            
            try:
                result = await api.delete_temp_code_user(
                    hass=hass,
                    entry_id=entry_id,
                    door_id=door_id,
                    pin_code=code,
                )
                
                if result.get("success"):
                    _LOGGER.info("Deleted temp code for door %d: %s", door_id, code)
                    
                    async_dispatcher_send(
                        hass,
                        f"{DISPATCH_TEMP_CODE}_{entry_id}",
                        {
                            "action": "delete",
                            "door_id": door_id,
                            "code": code,
                        }
                    )
                    
                    results.append({"device_id": device_id, "door_id": door_id, "success": True})
                else:
                    _LOGGER.error("Failed to delete temp code: %s", result.get("error"))
                    results.append({"device_id": device_id, "door_id": door_id, "success": False, "error": result.get("error", "Unknown error")})
                    all_success = False
                    
            except Exception as e:
                _LOGGER.exception("Error deleting temp code: %s", e)
                results.append({"device_id": device_id, "success": False, "error": str(e)})
                all_success = False
        
        # Single device backward compatibility
        if len(device_ids) == 1:
            if all_success:
                return {"success": True, "code": code, "door_id": results[0].get("door_id")}
            else:
                return {"success": False, "error": results[0].get("error", "Unknown error")}
        
        return {"success": all_success, "code": code, "results": results}
    
    async def handle_delete_temp_code_by_name(call: ServiceCall) -> dict[str, Any]:
        """Handle the delete_temp_code_by_name service call - finds code by name from sensor."""
        from . import api
        
        device_ids = _normalize_device_ids(call.data["door_device_id"])
        code_name = call.data["code_name"]
        force_remove = call.data.get("force_remove", False)
        
        results = []
        all_success = True
        
        for device_id in device_ids:
            entry_id, door_id = _get_door_id_from_device(hass, device_id)
            if entry_id is None or door_id is None:
                _LOGGER.error("Could not determine door from device %s", device_id)
                results.append({"device_id": device_id, "success": False, "error": "Invalid door"})
                all_success = False
                continue
            
            if entry_id not in hass.data.get(DOMAIN, {}):
                _LOGGER.error("Entry %s not found in domain data", entry_id)
                results.append({"device_id": device_id, "success": False, "error": "Integration not configured"})
                all_success = False
                continue
            
            # Find the temp code sensor for this door to look up the code by name
            code = None
            for state in hass.states.async_all():
                if not state.entity_id.endswith("_temp_code"):
                    continue
                
                attrs = state.attributes or {}
                if attrs.get("door_id") != door_id:
                    continue
                
                active_codes = attrs.get("active_codes", [])
                for code_entry in active_codes:
                    if code_entry.get("code_name") == code_name:
                        code = code_entry.get("code")
                        break
                break
            
            if not code:
                _LOGGER.warning("No code found with name '%s' for door %d", code_name, door_id)
                results.append({"device_id": device_id, "door_id": door_id, "success": False, "error": f"No code found with name '{code_name}'"})
                # Don't mark as failure if force_remove - just skip this door
                if not force_remove:
                    all_success = False
                continue
            
            try:
                result = await api.delete_temp_code_user(
                    hass=hass,
                    entry_id=entry_id,
                    door_id=door_id,
                    pin_code=code,
                )
                
                if result.get("success"):
                    _LOGGER.info("Deleted temp code '%s' for door %d: %s", code_name, door_id, code)
                    
                    async_dispatcher_send(
                        hass,
                        f"{DISPATCH_TEMP_CODE}_{entry_id}",
                        {
                            "action": "delete",
                            "door_id": door_id,
                            "code": code,
                        }
                    )
                    
                    results.append({"device_id": device_id, "door_id": door_id, "code": code, "success": True})
                else:
                    error_msg = result.get("error", "Unknown error")
                    _LOGGER.warning("Hartmann deletion failed for '%s': %s", code_name, error_msg)
                    
                    if force_remove:
                        _LOGGER.info("Force removing '%s' from sensor despite Hartmann error", code_name)
                        async_dispatcher_send(
                            hass,
                            f"{DISPATCH_TEMP_CODE}_{entry_id}",
                            {
                                "action": "delete",
                                "door_id": door_id,
                                "code": code,
                            }
                        )
                        results.append({"device_id": device_id, "door_id": door_id, "code": code, "success": True, "warning": f"Removed from sensor but Hartmann failed: {error_msg}"})
                    else:
                        results.append({"device_id": device_id, "door_id": door_id, "success": False, "error": error_msg})
                        all_success = False
                    
            except Exception as e:
                _LOGGER.exception("Error deleting temp code by name: %s", e)
                
                if force_remove and code:
                    _LOGGER.info("Force removing '%s' from sensor despite error", code_name)
                    async_dispatcher_send(
                        hass,
                        f"{DISPATCH_TEMP_CODE}_{entry_id}",
                        {
                            "action": "delete",
                            "door_id": door_id,
                            "code": code,
                        }
                    )
                    results.append({"device_id": device_id, "door_id": door_id, "code": code, "success": True, "warning": f"Removed from sensor but error occurred: {e}"})
                else:
                    results.append({"device_id": device_id, "success": False, "error": str(e)})
                    all_success = False
        
        # Single device backward compatibility
        if len(device_ids) == 1:
            r = results[0] if results else {"success": False, "error": "No results"}
            if r.get("success"):
                return {"success": True, "code": r.get("code"), "code_name": code_name, "door_id": r.get("door_id"), "warning": r.get("warning")}
            else:
                return {"success": False, "error": r.get("error", "Unknown error")}
        
        return {"success": all_success, "code_name": code_name, "results": results}
    
    async def handle_clear_all_temp_codes(call: ServiceCall) -> dict[str, Any]:
        """Handle the clear_all_temp_codes service call - removes all codes from sensor."""
        from . import api
        
        device_ids = _normalize_device_ids(call.data["door_device_id"])
        
        results = []
        total_cleared = 0
        
        for device_id in device_ids:
            entry_id, door_id = _get_door_id_from_device(hass, device_id)
            if entry_id is None or door_id is None:
                _LOGGER.error("Could not determine door from device %s", device_id)
                results.append({"device_id": device_id, "success": False, "error": "Invalid door", "cleared": 0})
                continue
            
            # Find the temp code sensor for this door
            active_codes = []
            for state in hass.states.async_all():
                if not state.entity_id.endswith("_temp_code"):
                    continue
                
                attrs = state.attributes or {}
                if attrs.get("door_id") != door_id:
                    continue
                
                active_codes = attrs.get("active_codes", [])
                break
            
            if not active_codes:
                _LOGGER.info("No active codes to clear for door %d", door_id)
                results.append({"device_id": device_id, "door_id": door_id, "success": True, "cleared": 0})
                continue
            
            cleared_count = 0
            errors = []
            
            for code_entry in active_codes:
                code = code_entry.get("code")
                code_name = code_entry.get("code_name", "Unknown")
                
                if not code:
                    continue
                
                try:
                    result = await api.delete_temp_code_user(
                        hass=hass,
                        entry_id=entry_id,
                        door_id=door_id,
                        pin_code=code,
                    )
                    
                    if result.get("success"):
                        _LOGGER.info("Deleted temp code '%s' from Hartmann", code_name)
                    else:
                        _LOGGER.warning("Hartmann deletion failed for '%s': %s", code_name, result.get("error"))
                    
                except Exception as e:
                    _LOGGER.warning("Error deleting '%s' from Hartmann: %s", code_name, e)
                    errors.append(f"{code_name}: {e}")
                
                # Always remove from sensor regardless of Hartmann result
                async_dispatcher_send(
                    hass,
                    f"{DISPATCH_TEMP_CODE}_{entry_id}",
                    {
                        "action": "delete",
                        "door_id": door_id,
                        "code": code,
                    }
                )
                cleared_count += 1
            
            _LOGGER.info("Cleared %d temp codes for door %d", cleared_count, door_id)
            total_cleared += cleared_count
            
            r = {"device_id": device_id, "door_id": door_id, "success": True, "cleared": cleared_count}
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
        """Handle the update_temp_code service call - update ExpiresOn/StartedOn."""
        from . import api
        
        device_ids = _normalize_device_ids(call.data["door_device_id"])
        code_name = call.data["code_name"]
        new_end_time = call.data.get("end_time")
        new_start_time = call.data.get("start_time")
        
        if not new_end_time and not new_start_time:
            return {"success": False, "error": "Must provide at least one of start_time or end_time"}
        
        # Find user_id from any temp_code sensor's active_codes
        user_id = None
        target_entry_id = None
        target_door_id = None
        
        for device_id in device_ids:
            entry_id, door_id = _get_door_id_from_device(hass, device_id)
            if entry_id is None or door_id is None:
                continue
            target_entry_id = entry_id
            target_door_id = door_id
            
            for entity_id in hass.states.async_entity_ids("sensor"):
                if not entity_id.endswith("_temp_code"):
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
            
            # Update sensor's stored end_time/start_time
            async_dispatcher_send(
                hass,
                f"{DISPATCH_TEMP_CODE}_{target_entry_id}",
                {
                    "action": "update",
                    "door_id": target_door_id,
                    "code_name": code_name,
                    "end_time": new_end_time,
                    "start_time": new_start_time,
                    "timestamp": datetime.now().isoformat(),
                }
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
        
        device_ids = _normalize_device_ids(call.data["door_device_id"])
        mode = call.data.get("mode", "Unlock")
        override_type = call.data.get("override_type", "until_resumed")
        minutes = call.data.get("minutes")
        
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
        
        # Group doors by entry_id for efficient API calls
        doors_by_entry: dict[str, list[int]] = {}
        invalid_devices = []
        
        for device_id in device_ids:
            entry_id, door_id = _get_door_id_from_device(hass, device_id)
            if entry_id is None or door_id is None:
                invalid_devices.append(device_id)
                continue
            
            if entry_id not in doors_by_entry:
                doors_by_entry[entry_id] = []
            doors_by_entry[entry_id].append(door_id)
        
        if not doors_by_entry:
            return {"success": False, "error": "No valid doors found"}
        
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
        
        # Single device backward compatibility
        if len(device_ids) == 1 and len(doors_by_entry) == 1:
            entry_id = list(doors_by_entry.keys())[0]
            door_id = doors_by_entry[entry_id][0]
            return {
                "success": all_success,
                "door_id": door_id,
                "mode": mode,
                "override_type": override_type,
                "minutes": minutes_arg,
            }
        
        return {
            "success": all_success,
            "mode": mode,
            "override_type": override_type,
            "minutes": minutes_arg,
            "results": results,
        }
    
    async def handle_resume_door(call: ServiceCall) -> dict[str, Any]:
        """Handle the resume_door service call - resume normal schedule."""
        from . import api
        
        device_ids = _normalize_device_ids(call.data["door_device_id"])
        
        # Group doors by entry_id for efficient API calls
        doors_by_entry: dict[str, list[int]] = {}
        
        for device_id in device_ids:
            entry_id, door_id = _get_door_id_from_device(hass, device_id)
            if entry_id is None or door_id is None:
                continue
            
            if entry_id not in doors_by_entry:
                doors_by_entry[entry_id] = []
            doors_by_entry[entry_id].append(door_id)
        
        if not doors_by_entry:
            return {"success": False, "error": "No valid doors found"}
        
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
        
        # Single device backward compatibility
        if len(device_ids) == 1 and len(doors_by_entry) == 1:
            entry_id = list(doors_by_entry.keys())[0]
            door_id = doors_by_entry[entry_id][0]
            return {"success": all_success, "door_id": door_id}
        
        return {"success": all_success, "results": results}
    
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
    
    _LOGGER.info("Registered Hartmann Control services (temp codes + OTR schedules + override/resume)")


async def async_unload_services(hass: HomeAssistant) -> None:
    """Unload Hartmann Control services."""
    hass.services.async_remove(DOMAIN, SERVICE_CREATE_TEMP_CODE)
    hass.services.async_remove(DOMAIN, SERVICE_DELETE_TEMP_CODE)
    hass.services.async_remove(DOMAIN, SERVICE_DELETE_TEMP_CODE_BY_NAME)
    hass.services.async_remove(DOMAIN, SERVICE_CLEAR_ALL_TEMP_CODES)
    hass.services.async_remove(DOMAIN, SERVICE_UPDATE_TEMP_CODE)
    hass.services.async_remove(DOMAIN, SERVICE_UPDATE_PANELS)
    hass.services.async_remove(DOMAIN, SERVICE_CREATE_OTR_SCHEDULE)
    hass.services.async_remove(DOMAIN, SERVICE_DELETE_OTR_SCHEDULE)
    hass.services.async_remove(DOMAIN, SERVICE_GET_OTR_SCHEDULES)
    hass.services.async_remove(DOMAIN, SERVICE_OVERRIDE_DOOR)
    hass.services.async_remove(DOMAIN, SERVICE_RESUME_DOOR)
    _LOGGER.info("Unregistered Hartmann Control services")

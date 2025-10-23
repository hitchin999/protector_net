# custom_components/protector_net/api.py

from __future__ import annotations

import httpx
import logging
import json
from typing import Iterable, Optional, Dict, Any, List

from aiohttp import ClientError

_LOGGER = logging.getLogger(__name__)
DOMAIN = "protector_net"


from .const import DOMAIN, FRIENDLY_TO_TZ_INDEX, OVERRIDE_MODE_LABEL_TO_TOKEN

_LOGGER = logging.getLogger(f"{DOMAIN}.api")

# Map API tokens to the controller "timeZone" index
_TOKEN_TO_INDEX = {
    "Lockdown": 0,
    "Card": 1,
    "Pin": 2,
    "CardOrPin": 3,
    "CardAndPin": 4,
    "Unlock": 5,
    "FirstCredentialIn": 6,
    "DualCredential": 7,
}

async def login(hass, base_url: str, username: str, password: str) -> str:
    """
    POST to /auth and return the ss-id session cookie.
    (Used internally by _request_with_reauth on 401.)
    """
    async with httpx.AsyncClient(verify=False) as client:
        resp = await client.post(
            f"{base_url}/auth",
            json={"Username": username, "Password": password},
            timeout=10,
        )
        resp.raise_for_status()
        for name, val in client.cookies.items():
            if name == "ss-id":
                _LOGGER.debug("Login successful, got ss-id")
                return val
    raise RuntimeError("Login succeeded but no ss-id cookie found")


async def _request_with_reauth(
    hass,
    entry_id: str,
    method: str,
    url: str,
    **kwargs
) -> httpx.Response:
    """
    Internal: send request with ss-id cookie; on 401, re-login and retry once.
    """
    cfg = hass.data[DOMAIN][entry_id]
    session = cfg["session_cookie"]
    headers = kwargs.pop("headers", {})
    headers["Content-Type"] = "application/json"
    headers["Cookie"]       = f"ss-id={session}"

    async with httpx.AsyncClient(verify=False) as client:
        resp = await client.request(method, url, headers=headers, **kwargs)
        if resp.status_code != 401:
            resp.raise_for_status()
            return resp

        # Session expired → re-authenticate
        _LOGGER.debug("%s: session expired, re-authenticating", entry_id)
        new_cookie = await login(
            hass,
            cfg["base_url"],
            cfg["username"],
            cfg["password"],
        )
        cfg["session_cookie"] = new_cookie
        headers["Cookie"] = f"ss-id={new_cookie}"
        resp = await client.request(method, url, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp


# -----------------------
# Partition / Doors
# -----------------------

async def get_partitions(
    hass,
    base_url: str,
    session_cookie: str
) -> list[dict]:
    """
    Used in config_flow: fetch partitions via cookie-auth.
    """
    headers = {"Content-Type": "application/json", "Cookie": f"ss-id={session_cookie}"}
    params = {"PageNumber": 1, "PerPage": 500}
    url = f"{base_url}/api/Partitions/ByPrivilege/Manage_Doors"
    async with httpx.AsyncClient(verify=False) as client:
        resp = await client.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json().get("Results", [])


async def get_all_doors(
    hass,
    entry_id: str
) -> list[dict]:
    """
    Fetch the doors for the given entry’s partition.
    """
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/doors"
    params = {"PartitionId": cfg['partition_id'], "PageNumber": 1, "PerPage": 500}
    try:
        resp = await _request_with_reauth(hass, entry_id, "GET", url, params=params, timeout=10)
        return resp.json().get("Results", [])
    except Exception as e:
        _LOGGER.exception("%s: Error fetching doors: %s", entry_id, e)
        return []


# -----------------------
# Door Commands
# -----------------------

async def pulse_unlock(
    hass,
    entry_id: str,
    door_ids: list[int]
) -> bool:
    """
    Pulse doors via PanelCommands/PulseDoor.
    """
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/PanelCommands/PulseDoor"
    payload = {"DoorIds": door_ids}
    try:
        await _request_with_reauth(hass, entry_id, "POST", url, json=payload, timeout=10)
        _LOGGER.info("%s: Pulse unlock sent for doors %s", entry_id, door_ids)
        return True
    except Exception as e:
        _LOGGER.error("%s: Error in pulse_unlock: %s", entry_id, e)
        return False


# (Legacy helper used by older buttons; prefers Unlock mode by default.)
async def set_override(
    hass,
    entry_id: str,
    door_ids: list[int],
    override_type: str,
    minutes: int | None = None
) -> bool:
    """
    Override doors via PanelCommands/OverrideDoor with TimeZoneMode='Unlock'.
    Prefer using apply_override() for full control.
    """
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/PanelCommands/OverrideDoor"
    payload: Dict[str, Any] = {"OverrideType": override_type, "DoorIds": door_ids, "TimeZoneMode": "Unlock"}
    if override_type == "Time":
        payload["Minutes"] = minutes or cfg.get("override_minutes")
    try:
        await _request_with_reauth(hass, entry_id, "POST", url, json=payload, timeout=10)
        _LOGGER.info("%s: Override %s sent to doors %s", entry_id, override_type, door_ids)
        return True
    except Exception as e:
        _LOGGER.error("%s: Error in set_override: %s", entry_id, e)
        return False

async def apply_override(
    hass,
    entry_id: str,
    door_ids: List[int],
    *,
    override_type: str,   # "Time" | "Resume" | "Schedule"
    mode: str,            # "Card"|"Pin"|"Unlock"|"CardAndPin"|"CardOrPin"|"FirstCredentialIn"|"DualCredential"|"Lockdown"
    minutes: int | None = None,
) -> bool:
    """
    Apply an override.

    We send BOTH the string token and an index for max compatibility, and we
    alias the two special modes to the exact tokens the server uses.
    """
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/PanelCommands/OverrideDoor"

    # Server-friendly token aliases for special modes (based on your WS samples):
    #   First Credential In -> UNLOCKWITHFIRSTCARDIN
    #   Dual Credential     -> DUALCARD
    token_alias = {
        "FirstCredentialIn": "UnlockWithFirstCardIn",
        "DualCredential": "DualCard",
    }
    token_to_send = token_alias.get(mode, mode)

    # Try to compute a best index for the mode (used by many panels)
    # Prefer the legend cached at startup if available; fall back to static map.
    legend_rev: Dict[str, int] = (hass.data[DOMAIN].get(entry_id, {}).get("tz_name_to_index") or {})

    # Convert token -> a friendly label we can look up (e.g., "CardOrPin" -> "Card or Pin")
    friendly_guess: Optional[str] = None
    for lbl, tok in OVERRIDE_MODE_LABEL_TO_TOKEN.items():
        if tok == mode:
            friendly_guess = {"unlock": "Unlock"}.get(lbl, lbl.title())
            break

    idx: Optional[int] = None
    if friendly_guess:
        idx = legend_rev.get(friendly_guess.lower())
        if idx is None:
            idx = FRIENDLY_TO_TZ_INDEX.get(friendly_guess)

    if idx is None:
        # Last-resort static token → index
        token_to_index = {
            "Lockdown": 0,
            "Card": 1,
            "Pin": 2,
            "CardOrPin": 3,
            "CardAndPin": 4,
            "Unlock": 5,
            "FirstCredentialIn": 6,
            "DualCredential": 7,
        }
        idx = token_to_index.get(mode)

    payload: Dict[str, Any] = {
        "DoorIds": door_ids,
        "OverrideType": override_type,
        "TimeZoneMode": token_to_send,   # string token
    }
    if override_type == "Time":
        payload["Minutes"] = int(minutes or cfg.get("override_minutes"))

    # Numeric forms for maximum compatibility (fixes First/Dual on some servers)
    if idx is not None:
        payload["ModeIndex"] = int(idx)
        payload["TimeZoneModeIndex"] = int(idx)  # some servers expect this exact key
        payload["TimeZone"] = int(idx)           # legacy/compat
        payload["TimeZoneState"] = int(idx)      # legacy/compat

    try:
        await _request_with_reauth(hass, entry_id, "POST", url, json=payload, timeout=10)
        _LOGGER.info(
            "%s: Apply override type=%s mode=%s (alias=%s idx=%s) minutes=%s doors=%s",
            entry_id, override_type, mode, token_to_send, idx, payload.get("Minutes"), door_ids
        )
        return True
    except Exception as e:
        _LOGGER.error("%s: Error in apply_override: %s (payload=%s)", entry_id, e, payload)
        return False
        
async def override_until_resume_card_or_pin(
    hass,
    entry_id: str,
    door_ids: list[int]
) -> bool:
    """
    Override doors until resume via CardOrPin (kept for backwards compatibility).
    Prefer apply_override(..., override_type='Resume', mode='CardOrPin').
    """
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/PanelCommands/OverrideDoor"
    payload = {"DoorIds": door_ids, "OverrideType": "Resume", "TimeZoneMode": "CardOrPin"}
    try:
        await _request_with_reauth(hass, entry_id, "POST", url, json=payload, timeout=10)
        _LOGGER.info("%s: Override CardOrPin sent to doors %s", entry_id, door_ids)
        return True
    except Exception as e:
        _LOGGER.error("%s: Error in override_until_resume_card_or_pin: %s", entry_id, e)
        return False

async def resume_schedule(
    hass,
    entry_id: str,
    door_ids: list[int]
) -> bool:
    """
    Resume door schedule via PanelCommands/ResumeDoor.
    """
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/PanelCommands/ResumeDoor"
    payload = {"DoorIds": door_ids}
    try:
        await _request_with_reauth(hass, entry_id, "POST", url, json=payload, timeout=10)
        _LOGGER.info("%s: Resumed schedule for doors %s", entry_id, door_ids)
        return True
    except Exception as e:
        _LOGGER.error("%s: Error in resume_schedule: %s", entry_id, e)
        return False


# -----------------------
# Action Plans
# -----------------------

async def get_action_plans(
    hass,
    *args
) -> list[dict]:
    """
    Overloaded: config_flow vs runtime.
    """
    import httpx
    if len(args) == 3:
        base, cookie, part = args
        url = f"{base}/api/ActionPlans"
        headers = {"Content-Type": "application/json", "Cookie": f"ss-id={cookie}"}
        params = {"PartitionId": part, "PageNumber": 1, "PerPage": 500}
        try:
            async with httpx.AsyncClient(verify=False) as client:
                r = await client.get(url, headers=headers, params=params, timeout=10)
                r.raise_for_status()
                return r.json().get("Results", [])
        except Exception as e:
            _LOGGER.error("Error fetching action plans (config_flow): %s", e)
            return []
    entry_id = args[0]
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/ActionPlans"
    params = {"PartitionId": cfg['partition_id'], "PageNumber": 1, "PerPage": 500}
    try:
        resp = await _request_with_reauth(hass, entry_id, "GET", url, params=params, timeout=10)
        return resp.json().get("Results", [])
    except Exception as e:
        _LOGGER.error("%s: Error fetching action plans: %s", entry_id, e)
        return []


async def get_action_plan_detail(
    hass,
    entry_id: str,
    plan_id: int
) -> dict:
    """
    Retrieve full plan (including Contents).
    """
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/ActionPlans/{plan_id}"
    resp = await _request_with_reauth(hass, entry_id, "GET", url, timeout=10)
    return resp.json()


async def find_or_clone_system_plan(
    hass,
    entry_id: str,
    trigger_id: int
) -> int:
    """
    Return existing System clone ID or clone+populate it.
    """
    cfg = hass.data[DOMAIN][entry_id]
    orig = await get_action_plan_detail(hass, entry_id, trigger_id)
    plan = orig.get("Result", {})
    # -- START patch: avoid double‐appending the marker --
    marker = " (Home Assistant)"
    orig_name = plan.get("Name", "")
    # If this plan already is our HA clone, return it immediately
    if orig_name.endswith(marker) and plan.get("PlanType") == "System":
        return plan.get("Id")
    # Strip any stray markers just in case
    if marker in orig_name:
        orig_name = orig_name.replace(marker, "")
    clone_name = f"{orig_name}{marker}"
    # -- END patch --
    existing = await get_action_plans(hass, entry_id)
    for p in existing:
        if p.get('PlanType') == 'System' and p.get('Name') == clone_name and p.get('PartitionId') == plan.get('PartitionId'):
            return p.get('Id')
    # 1) create skeleton
    payload = {
        "PlanType":     "System",
        "Name":         clone_name,
        "Description":  plan.get('Description'),
        "HighSecurity": plan.get('HighSecurity', False),
        "PartitionId":  plan.get('PartitionId'),
    }
    resp = await _request_with_reauth(
        hass, entry_id, "POST", f"{cfg['base_url']}/api/ActionPlans", json=payload, timeout=10
    )
    new_id = resp.json().get('Id')
    # 2) populate Contents via PUT
    put_body = {
        "Id": new_id,
        "Properties": [ {"Name": "Contents", "Value": plan.get('Contents', '')} ]
    }
    await _request_with_reauth(
        hass, entry_id, "PUT", f"{cfg['base_url']}/api/ActionPlans/{new_id}", json=put_body, timeout=10
    )
    return new_id


async def execute_action_plan(
    hass,
    entry_id: str,
    plan_id: int,
    log_level: str | None = None,
    variables: dict | None = None
) -> bool:
    """
    Execute a single action plan by ID with optional SessionVars.
    """
    cfg = hass.data[DOMAIN][entry_id]
    path = f"/api/ActionPlans/{plan_id}/Exec"
    if log_level:
        path += f"/{log_level}"
    url = f"{cfg['base_url']}{path}?PartitionId={cfg['partition_id']}"
    body = {"SessionVars": variables or {}}
    try:
        await _request_with_reauth(hass, entry_id, "POST", url, json=body, timeout=10)
        _LOGGER.info("%s: Executed action plan %s", entry_id, plan_id)
        return True
    except Exception as e:
        _LOGGER.error("%s: Error executing action plan %s: %s", entry_id, plan_id, e)
        return False


async def find_or_create_ha_log_plan(hass, entry_id: str) -> int:
    """
    Ensure a single System plan called “HA Door Log” exists, and return its ID.
    """
    cfg = hass.data[DOMAIN][entry_id]
    marker_name = "HA Door Log"

    # 1) Fetch all existing plans
    all_plans = await get_action_plans(hass, entry_id)
    for p in all_plans:
        if (
            p["PlanType"] == "System"
            and p["Name"] == marker_name
            and p["PartitionId"] == cfg["partition_id"]
        ):
            return p["Id"]

    # 2) Not found → create skeleton
    payload = {
        "PlanType":     "System",
        "Name":         marker_name,
        "Description":  "Log each Home Assistant door button press",
        "HighSecurity": False,
        "PartitionId":  cfg["partition_id"],
    }
    resp = await _request_with_reauth(
        hass,
        entry_id,
        "POST",
        f"{cfg['base_url']}/api/ActionPlans",
        json=payload,
        timeout=10,
    )
    plan_id = resp.json()["Id"]

    # 3) Populate its Contents via PUT
    content = {
        "InitVar": {},
        "Action": {
            "_Type": "Log",
            "Parameters": {
                "Level":   1,
                "Message": "@{Session.App} unlocked @{Session.Door}"
            },
            "Fail":   None,
            "Always": None,
            "Then":   None
        }
    }
    put_body = {
        "Id":         plan_id,
        "Properties": [
            {"Name": "Contents", "Value": json.dumps(content)}
        ]
    }
    await _request_with_reauth(
        hass,
        entry_id,
        "PUT",
        f"{cfg['base_url']}/api/ActionPlans/{plan_id}",
        json=put_body,
        timeout=10,
    )

    return plan_id


# -----------------------
# System / Maps
# -----------------------

async def get_system_overview(hass, entry_id: str) -> dict:
    """Return the /api/system/overview/System payload (top-level dict)."""
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/system/overview/System"
    resp = await _request_with_reauth(hass, entry_id, "GET", url, timeout=15)
    return resp.json()  # caller will walk ["Status"]["Nodes"]


async def get_door_time_zone_states(hass, entry_id: str) -> dict[int, dict]:
    """
    Returns {index: {name,color,...}} for DoorTimeZoneMode (legend for WS timeZone).
    """
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/TimeSpanStates/DoorTimeZoneMode"
    resp = await _request_with_reauth(hass, entry_id, "GET", url, timeout=10)
    items = resp.json()  # [{index,name,color,...}, ...]
    return {int(x["index"]): x for x in items if "index" in x}


from typing import Dict

async def build_statusid_to_doorid_map(hass, entry_id: str) -> Dict[str, int]:
    """
    Build a {StatusId -> DoorId} map from /api/system/overview/System so
    websocket 'status' frames can be routed to the correct door.
    """
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/system/overview/System"
    resp = await _request_with_reauth(hass, entry_id, "GET", url, timeout=15)
    data = resp.json() or {}

    result: Dict[str, int] = {}

    def walk(node):
        if not isinstance(node, dict):
            return
        if node.get("Type") == "Door":
            sid = node.get("StatusId")
            did = node.get("Id")
            if sid and did is not None:
                result[sid] = int(did)
        for child in node.get("Nodes", []):
            walk(child)

    walk(data.get("Status", {}))
    return result

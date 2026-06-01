# custom_components/protector_net/api.py

from __future__ import annotations

import httpx
import logging
import json
from typing import Iterable, Optional, Dict, Any, List

from homeassistant.exceptions import ConfigEntryAuthFailed

_LOGGER = logging.getLogger(__name__)

from .const import DOMAIN, FRIENDLY_TO_TZ_INDEX, OVERRIDE_MODE_LABEL_TO_TOKEN

_LOGGER = logging.getLogger(f"{DOMAIN}.api")


def _is_transient_outage(exc: BaseException) -> bool:
    """Return True if the exception looks like a server-down/restart event.

    Mirror of the helper in ws.py. Used to downgrade routine connection
    failures (e.g. nightly Hartmann box reboots) from WARNING to INFO so
    they don't pollute HA's notification surface.
    """
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    # httpx network-class errors (covers Connect*/Read*/Write*/Pool*Timeout
    # and most NetworkError subclasses across httpx versions).
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError,
                        httpx.RemoteProtocolError, httpx.TransportError,
                        httpx.HTTPError)):
        # HTTPError is broad — only treat as transient if no response was
        # received (i.e. server didn't answer at all). HTTPStatusError still
        # has a response and stays at WARNING.
        if isinstance(exc, httpx.HTTPStatusError):
            # 502/503/504 from a bouncing server are also transient.
            try:
                code = exc.response.status_code
                if code in (502, 503, 504):
                    return True
            except Exception:
                pass
            return False
        return True
    # Defensive name-based match for anything we missed (different httpx
    # versions, wrapper exceptions, etc.).
    cls_name = type(exc).__name__.lower()
    if any(tok in cls_name for tok in ("timeout", "connect", "disconnect",
                                       "unreachable", "remoteprotocol")):
        return True
    msg = str(exc).lower()
    if "connection timeout" in msg or "cannot connect to host" in msg:
        return True
    return False

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
        try:
            new_cookie = await login(
                hass,
                cfg["base_url"],
                cfg["username"],
                cfg["password"],
            )
        except httpx.HTTPStatusError as err:
            # The /auth call itself was rejected. If it's 401/403, the stored
            # credentials are no longer valid — surface as ConfigEntryAuthFailed
            # so HA shows a "Reauthenticate" repair notification instead of a
            # noisy stack trace. Other status codes (5xx, etc.) propagate.
            if err.response.status_code in (401, 403):
                _LOGGER.warning(
                    "%s: stored credentials rejected by server (HTTP %s) — "
                    "prompting user to reauthenticate",
                    entry_id, err.response.status_code,
                )
                raise ConfigEntryAuthFailed(
                    "Stored Protector.Net/Odyssey credentials were rejected"
                ) from err
            raise

        cfg["session_cookie"] = new_cookie
        headers["Cookie"] = f"ss-id={new_cookie}"
        resp = await client.request(method, url, headers=headers, **kwargs)
        # Belt-and-suspenders: if the brand-new cookie still yields 401, the
        # account most likely lacks permission or was disabled. Treat as auth
        # failure rather than retrying forever.
        if resp.status_code == 401:
            _LOGGER.warning(
                "%s: request still 401 after fresh login — credentials likely invalid",
                entry_id,
            )
            raise ConfigEntryAuthFailed(
                "Re-login succeeded but request still returned 401"
            )
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
        _LOGGER.warning("%s: Error fetching doors: %s", entry_id, e)
        return []


async def get_partition_name(hass, entry_id: str) -> str | None:
    """Look up the current Hartmann name for this entry's partition.

    Used by the post-load name sync to detect partition renames in Hartmann.
    Falls back to the list endpoint (rather than /api/Partitions/{id}) because
    the list endpoint is the same one config_flow uses, so we know it's
    available with the same auth on both Odyssey and Protector.Net.
    """
    cfg = hass.data[DOMAIN][entry_id]
    pid = cfg.get("partition_id")
    if not pid:
        return None
    url = f"{cfg['base_url']}/api/Partitions/ByPrivilege/Manage_Doors"
    params = {"PageNumber": 1, "PerPage": 500}
    try:
        resp = await _request_with_reauth(hass, entry_id, "GET", url, params=params, timeout=10)
        for p in resp.json().get("Results", []) or []:
            try:
                if int(p.get("Id", -1)) == int(pid):
                    name = p.get("Name")
                    return str(name) if name else None
            except (TypeError, ValueError):
                continue
        return None
    except Exception as e:
        _LOGGER.debug("%s: Error fetching partition name: %s", entry_id, e)
        return None

async def get_available_readers(hass, entry_id: str) -> list[dict]:
    """Return partition-scoped readers -> doors (fixes Reader 2 / in-out readers)."""
    cfg = hass.data[DOMAIN][entry_id]
    base_url = cfg["base_url"]
    partition_id = cfg.get("partition_id")

    if not partition_id:
        _LOGGER.debug("%s: get_available_readers: no partition_id in cfg", entry_id)
        return []

    url = f"{base_url}/api/AccessPrivilegeGroups/AvailableReaders/{partition_id}"
    params = {"PageNumber": 1, "PerPage": 500}

    try:
        resp = await _request_with_reauth(
            hass,
            entry_id,
            "GET",
            url,
            params=params,
            timeout=10,
        )
        data = resp.json() or {}
    except Exception as e:
        _LOGGER.warning("%s: get_available_readers failed: %s", entry_id, e)
        return []

    results = data.get("Results") or []
    _LOGGER.debug("%s: get_available_readers: got %d items", entry_id, len(results))
    return results

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
    
async def get_door_status(
    hass,
    entry_id: str,
    door_id: int
) -> Optional[dict]:
    """
    Fetch current status for a single door.

    Odyssey servers expose:
        GET /api/Doors/{door_id}/Status
    Some variants may be case-insensitive; try lowercase fallback once.

    Returns a dict or None on error / unsupported.
    """
    cfg = hass.data[DOMAIN][entry_id]

    # Try canonical (PascalCase) path first
    url_main = f"{cfg['base_url']}/api/Doors/{door_id}/Status"
    try:
        resp = await _request_with_reauth(hass, entry_id, "GET", url_main, timeout=10)
        return resp.json()
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 404:
            # Fallback to lowercase path (some deployments differ)
            url_fallback = f"{cfg['base_url']}/api/doors/{door_id}/status"
            try:
                resp2 = await _request_with_reauth(hass, entry_id, "GET", url_fallback, timeout=10)
                return resp2.json()
            except httpx.HTTPStatusError as e2:
                if e2.response is not None and e2.response.status_code == 404:
                    # Not supported on this server (likely Protector.Net)
                    return None
                excepted = True
            except Exception:
                return None
            return None
        # Other HTTP errors – treat as unsupported for snapshot purposes
        return None
    except Exception:
        return None

from typing import Dict

# -----------------------
# Temp Code Management
# -----------------------

async def get_security_levels(hass, entry_id: str) -> list[dict]:
    """
    Fetch available security levels for user creation.
    """
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/SecurityLevels"
    params = {"PageNumber": 1, "PerPage": 100}
    try:
        resp = await _request_with_reauth(hass, entry_id, "GET", url, params=params, timeout=10)
        return resp.json().get("Results", [])
    except Exception as e:
        # This is expected on some Hartmann versions - we'll use default ID 1
        _LOGGER.debug("%s: Security levels endpoint not available (using default): %s", entry_id, e)
        return []


async def get_user_holiday_groups(hass, entry_id: str) -> list[dict]:
    """
    Fetch user holiday groups needed for APG creation.
    """
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/UserHolidayGroups"
    params = {"PageNumber": 1, "PerPage": 100}
    try:
        resp = await _request_with_reauth(hass, entry_id, "GET", url, params=params, timeout=10)
        return resp.json().get("Results", [])
    except Exception as e:
        _LOGGER.error("%s: Error fetching user holiday groups: %s", entry_id, e)
        return []


async def get_readers_for_door(hass, entry_id: str, door_id: int) -> list[dict]:
    """
    Get readers associated with a specific door from the available readers list.
    """
    cfg = hass.data[DOMAIN][entry_id]
    partition_id = cfg.get("partition_id")
    
    if not partition_id:
        _LOGGER.error("%s: No partition_id in config", entry_id)
        return []
    
    url = f"{cfg['base_url']}/api/AccessPrivilegeGroups/AvailableReaders/{partition_id}"
    params = {"PageNumber": 1, "PerPage": 500}
    
    try:
        resp = await _request_with_reauth(hass, entry_id, "GET", url, params=params, timeout=10)
        all_readers = resp.json().get("Results", [])
        
        # Filter readers that belong to this door
        door_readers = [r for r in all_readers if r.get("DoorId") == door_id]
        _LOGGER.debug("%s: Found %d readers for door %d", entry_id, len(door_readers), door_id)
        return door_readers
    except Exception as e:
        _LOGGER.error("%s: Error fetching readers for door %d: %s", entry_id, door_id, e)
        return []


async def get_access_privilege_groups(hass, entry_id: str) -> list[dict]:
    """
    Fetch all access privilege groups for the partition.
    """
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/AccessPrivilegeGroups"
    params = {"PartitionId": cfg.get("partition_id"), "PageNumber": 1, "PerPage": 500}
    try:
        resp = await _request_with_reauth(hass, entry_id, "GET", url, params=params, timeout=10)
        return resp.json().get("Results", [])
    except Exception as e:
        _LOGGER.error("%s: Error fetching access privilege groups: %s", entry_id, e)
        return []


async def get_user_time_zones(hass, entry_id: str) -> list[dict]:
    """
    Fetch available user time zones for reader access assignments.
    """
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/UserTimeZones"
    params = {"PageNumber": 1, "PerPage": 100}
    try:
        resp = await _request_with_reauth(hass, entry_id, "GET", url, params=params, timeout=10)
        return resp.json().get("Results", [])
    except Exception as e:
        _LOGGER.debug("%s: Error fetching user time zones: %s", entry_id, e)
        return []


async def get_always_access_timezone_id(hass, entry_id: str) -> int:
    """
    Find the TimeZoneId for "Always Access" or similar 24/7 access.
    Falls back to ID 2 if not found (common default for Always Access).
    """
    time_zones = await get_user_time_zones(hass, entry_id)
    
    # Look for common names for 24/7 access
    always_names = ["always access", "always", "24/7", "all day", "anytime", "no restriction"]
    
    for tz in time_zones:
        tz_name = (tz.get("Name") or "").lower()
        for name in always_names:
            if name in tz_name:
                tz_id = tz.get("Id")
                _LOGGER.debug("%s: Found 'Always Access' timezone: %s (ID: %d)", entry_id, tz.get("Name"), tz_id)
                return tz_id
    
    # If not found, log available zones and use default
    if time_zones:
        _LOGGER.debug("%s: Available time zones: %s", entry_id, 
                     [(tz.get("Id"), tz.get("Name")) for tz in time_zones])
    
    # Default to 2 (often "Always Access" in Hartmann)
    _LOGGER.debug("%s: Using default TimeZoneId 2 for Always Access", entry_id)
    return 2


async def find_or_create_temp_apg(
    hass,
    entry_id: str,
    door_id: int,
    door_name: str,
) -> Optional[int]:
    """
    Find or create an Access Privilege Group for temporary door access.
    Returns the APG ID or None on failure.
    """
    cfg = hass.data[DOMAIN][entry_id]
    partition_id = cfg.get("partition_id")
    
    apg_name = f"HA Temp Access - {door_name}"
    
    # First, check if APG already exists
    existing_apgs = await get_access_privilege_groups(hass, entry_id)
    for apg in existing_apgs:
        if apg.get("Name") == apg_name:
            apg_id = apg.get("Id")
            _LOGGER.debug("%s: Found existing APG '%s' (ID: %d)", entry_id, apg_name, apg_id)
            
            # Check if readers are assigned, if not, assign them
            readers_url = f"{cfg['base_url']}/api/AccessPrivilegeGroups/{apg_id}/Readers"
            try:
                resp = await _request_with_reauth(hass, entry_id, "GET", readers_url, timeout=10)
                assigned_readers = resp.json().get("Results", [])
                
                if not assigned_readers:
                    _LOGGER.info("%s: APG '%s' has no readers, assigning...", entry_id, apg_name)
                    
                    # Get readers for this door
                    door_readers = await get_readers_for_door(hass, entry_id, door_id)
                    always_access_tz_id = await get_always_access_timezone_id(hass, entry_id)
                    
                    for reader in door_readers:
                        reader_id = reader.get("Id")
                        if reader_id:
                            assign_url = f"{cfg['base_url']}/api/AccessPrivilegeGroups/{apg_id}/Readers/{reader_id}/{always_access_tz_id}"
                            try:
                                await _request_with_reauth(hass, entry_id, "PUT", assign_url, json={}, timeout=10)
                                _LOGGER.info("%s: Assigned reader %d to existing APG %d", entry_id, reader_id, apg_id)
                            except Exception as e:
                                _LOGGER.warning("%s: Failed to assign reader %d to APG: %s", entry_id, reader_id, e)
            except Exception as e:
                _LOGGER.debug("%s: Could not check APG readers: %s", entry_id, e)
            
            return apg_id
    
    # Get holiday time zone group (required for APG creation)
    holiday_groups = await get_user_holiday_groups(hass, entry_id)
    if not holiday_groups:
        _LOGGER.error("%s: No holiday time zone groups found, cannot create APG", entry_id)
        return None
    
    holiday_tz_group_id = holiday_groups[0].get("Id")
    
    # Get readers for this door
    door_readers = await get_readers_for_door(hass, entry_id, door_id)
    if not door_readers:
        _LOGGER.error("%s: No readers found for door %d, cannot create APG", entry_id, door_id)
        return None
    
    # Create the APG
    url = f"{cfg['base_url']}/api/AccessPrivilegeGroups"
    payload = {
        "GroupType": "Local",
        "Name": apg_name,
        "Description": f"Home Assistant temporary access for {door_name}",
        "HolidayTimeZoneGroupId": holiday_tz_group_id,
        "PartitionId": partition_id,
    }
    
    _LOGGER.debug("%s: Creating APG with payload: %s", entry_id, payload)
    
    try:
        resp = await _request_with_reauth(hass, entry_id, "POST", url, json=payload, timeout=10)
        result = resp.json()
        apg_id = result.get("Id")
        
        if not apg_id:
            _LOGGER.error("%s: APG creation returned no ID: %s", entry_id, result)
            return None
        
        _LOGGER.info("%s: Created APG '%s' (ID: %d)", entry_id, apg_name, apg_id)
        
        # Get the "Always Access" timezone ID for 24/7 door access
        always_access_tz_id = await get_always_access_timezone_id(hass, entry_id)
        
        # Assign readers to the APG with Always Access timezone
        for reader in door_readers:
            reader_id = reader.get("Id")
            if reader_id:
                assign_url = f"{cfg['base_url']}/api/AccessPrivilegeGroups/{apg_id}/Readers/{reader_id}/{always_access_tz_id}"
                try:
                    await _request_with_reauth(hass, entry_id, "PUT", assign_url, json={}, timeout=10)
                    _LOGGER.debug("%s: Assigned reader %d to APG %d with timezone %d", entry_id, reader_id, apg_id, always_access_tz_id)
                except Exception as e:
                    _LOGGER.warning("%s: Failed to assign reader %d to APG: %s", entry_id, reader_id, e)
        
        return apg_id
        
    except httpx.HTTPStatusError as e:
        error_body = ""
        try:
            error_body = e.response.text
        except Exception:
            pass
        _LOGGER.error("%s: Error creating APG: %s - Response: %s", entry_id, e, error_body)
        return None
    except Exception as e:
        _LOGGER.error("%s: Error creating APG: %s", entry_id, e)
        return None


async def get_partition_users(
    hass,
    entry_id: str,
    filter_str: Optional[str] = None
) -> list[dict]:
    """
    Get users in the partition, optionally filtered.
    """
    cfg = hass.data[DOMAIN][entry_id]
    partition_id = cfg.get("partition_id")
    
    url = f"{cfg['base_url']}/api/Partitions/{partition_id}/Users"
    params = {"PageNumber": 1, "PerPage": 500}
    if filter_str:
        params["Filter"] = filter_str
    
    try:
        resp = await _request_with_reauth(hass, entry_id, "GET", url, params=params, timeout=15)
        return resp.json().get("Results", [])
    except Exception as e:
        _LOGGER.error("%s: Error fetching partition users: %s", entry_id, e)
        return []


async def get_user_credentials(hass, entry_id: str, user_id: int) -> list[dict]:
    """
    Get credentials for a specific user.
    """
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/Users/{user_id}/Credentials"
    params = {"PageNumber": 1, "PerPage": 100}
    
    try:
        resp = await _request_with_reauth(hass, entry_id, "GET", url, params=params, timeout=10)
        return resp.json().get("Results", [])
    except Exception as e:
        _LOGGER.error("%s: Error fetching credentials for user %d: %s", entry_id, user_id, e)
        return []


def _convert_datetime_from_hartmann(dt_string: Optional[str], hass=None) -> Optional[str]:
    """
    Convert a datetime string returned by Hartmann (UTC) back to local time.
    Hartmann API returns datetimes in UTC without timezone info.
    
    Input: "2026-02-16T01:48:00" (UTC) -> Output: "2026-02-15T20:48:00" (local, e.g. EST)
    """
    if not dt_string:
        return None
    
    try:
        from datetime import datetime, timezone
        
        dt_str = str(dt_string).strip().replace('Z', '').replace('z', '')
        
        # Remove any trailing timezone offset (shouldn't be there but just in case)
        import re
        dt_str = re.sub(r'[+-]\d{2}:?\d{2}$', '', dt_str)
        
        for fmt in ["%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"]:
            try:
                dt_naive = datetime.strptime(dt_str[:26], fmt)
                # Treat as UTC
                dt_utc = dt_naive.replace(tzinfo=timezone.utc)
                
                # Convert to local timezone
                if hass is not None:
                    import zoneinfo
                    local_tz = zoneinfo.ZoneInfo(hass.config.time_zone)
                    dt_local = dt_utc.astimezone(local_tz)
                else:
                    dt_local = dt_utc.astimezone()  # System local TZ
                
                return dt_local.strftime("%Y-%m-%dT%H:%M:%S")
            except ValueError:
                continue
        
        return dt_str
        
    except Exception as e:
        _LOGGER.warning("Error converting datetime from Hartmann '%s': %s", dt_string, e)
        return dt_string


def _convert_datetime_for_hartmann(dt_string: Optional[str], hass=None) -> Optional[str]:
    """
    Convert an ISO datetime string to the format Hartmann expects.
    Hartmann treats incoming datetimes as UTC and converts to local time.
    So we need to send the UTC equivalent of the local time.
    
    Input: "2026-02-08T17:20:00-05:00" (5:20 PM EST) -> Output: "2026-02-08T22:20:00" (UTC)
    Input: "2026-02-08T17:20:00" (5:20 PM local, no TZ) -> Output: "2026-02-08T22:20:00" (UTC)
    """
    if not dt_string:
        return None
    
    try:
        from datetime import datetime, timezone
        
        dt_str = str(dt_string).strip()
        
        # Try to parse as ISO format with timezone
        try:
            dt = datetime.fromisoformat(dt_str)
            if dt.tzinfo is not None:
                # Has timezone info - convert to UTC
                dt_utc = dt.astimezone(timezone.utc)
                return dt_utc.strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            pass
        
        # No timezone info - assume local time and convert to UTC
        # Clean up the string first
        dt_str = dt_str.replace('Z', '').replace('z', '')
        
        # Remove any timezone offset manually
        import re
        dt_str = re.sub(r'[+-]\d{2}:?\d{2}$', '', dt_str)
        
        for fmt in ["%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"]:
            try:
                dt_naive = datetime.strptime(dt_str[:26], fmt)
                
                # Get local timezone and convert to UTC
                try:
                    if hass is not None:
                        # Use Home Assistant's timezone
                        import zoneinfo
                        local_tz = zoneinfo.ZoneInfo(hass.config.time_zone)
                        dt_local = dt_naive.replace(tzinfo=local_tz)
                        dt_utc = dt_local.astimezone(timezone.utc)
                        return dt_utc.strftime("%Y-%m-%dT%H:%M:%S")
                    else:
                        # Fallback: use system local timezone
                        dt_local = dt_naive.astimezone()  # Adds local TZ
                        dt_utc = dt_local.astimezone(timezone.utc)
                        return dt_utc.strftime("%Y-%m-%dT%H:%M:%S")
                except Exception as tz_err:
                    _LOGGER.warning("Error converting timezone: %s, returning as-is", tz_err)
                    return dt_naive.strftime("%Y-%m-%dT%H:%M:%S")
                    
            except ValueError:
                continue
        
        if len(dt_str) >= 19:
            return dt_str[:19]
        
        return dt_str
        
    except Exception as e:
        _LOGGER.warning("Error converting datetime '%s': %s", dt_string, e)
        return dt_string


async def create_temp_code_user(
    hass,
    entry_id: str,
    door_ids: list[int],
    code_name: str,
    pin_code: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> dict:
    """
    Create a temporary user with PIN-only access to one or more doors.

    The user is created **once** with a single PIN credential, then assigned to
    the access privilege group of each requested door. This avoids Hartmann's
    PIN-uniqueness rejection, which would otherwise block creating N separate
    users with the same PIN.

    Args:
        door_ids: list of Hartmann door IDs to grant access to. Must contain at
            least one ID.
        start_time: ISO datetime string when the code becomes active (optional).
        end_time: ISO datetime string when the code expires (optional).

    Returns:
        On success::

            {
                "success": True,
                "user_id": int,
                "doors": [
                    {"door_id": int, "success": bool, "error": Optional[str]},
                    ...
                ],
            }

        On failure (user/credential creation failed)::

            {"success": False, "error": str}
    """
    cfg = hass.data[DOMAIN][entry_id]
    partition_id = cfg.get("partition_id")

    if not door_ids:
        return {"success": False, "error": "No doors specified"}

    # Resolve door names for APG naming (one fetch covers all doors)
    doors = await get_all_doors(hass, entry_id)
    door_name_by_id: dict[int, str] = {}
    for d in doors:
        did = d.get("Id")
        if did is not None:
            door_name_by_id[int(did)] = d.get("Name") or f"Door {did}"

    # Pre-resolve / create APGs for every requested door BEFORE creating the
    # user. If any APG can't be obtained, we report it but still proceed for
    # the doors whose APGs are valid (so a single bad door doesn't block
    # others).
    apg_by_door: dict[int, Optional[int]] = {}
    for did in door_ids:
        dname = door_name_by_id.get(int(did)) or f"Door {did}"
        apg_id = await find_or_create_temp_apg(hass, entry_id, int(did), dname)
        apg_by_door[int(did)] = apg_id
        if not apg_id:
            _LOGGER.warning(
                "%s: Could not find/create APG for door %d (%s)",
                entry_id, did, dname
            )

    valid_apg_ids = [a for a in apg_by_door.values() if a]
    if not valid_apg_ids:
        return {
            "success": False,
            "error": "Failed to create/find Access Privilege Group for any door",
        }

    # Get security levels (use default if not available)
    security_levels = await get_security_levels(hass, entry_id)
    if not security_levels:
        security_level_id = 1
        _LOGGER.debug("%s: Using default security level ID 1", entry_id)
    else:
        security_level_id = security_levels[0].get("Id", 1)

    # Create unique user name with prefix for easy identification
    first_name = f"HA-{pin_code}"
    last_name = code_name[:60]  # Max 60 chars

    # Convert datetime strings to Hartmann format (UTC)
    hartmann_start = _convert_datetime_for_hartmann(start_time, hass)
    hartmann_end = _convert_datetime_for_hartmann(end_time, hass)

    # Create the user (AccessGroups is readOnly but still required, send empty array)
    url = f"{cfg['base_url']}/api/Users"
    payload = {
        "FirstName": first_name,
        "LastName": last_name,
        "SecurityLevelId": security_level_id,
        "Partitions": [partition_id],
        "AccessGroups": [],  # Required but readOnly - send empty, we assign APG separately
        "IsMaster": False,
        "IsSupervisor": False,
        "IsSecurity": False,
        "FirstCardInEnabled": False,
        "HandicapOpener": False,
        "CanTripleSwipe": False,
    }

    # Add time restrictions if provided
    if hartmann_start:
        payload["StartedOn"] = hartmann_start
    if hartmann_end:
        payload["ExpiresOn"] = hartmann_end

    _LOGGER.debug(
        "%s: Creating user with StartedOn=%s, ExpiresOn=%s for %d door(s)",
        entry_id, hartmann_start, hartmann_end, len(door_ids)
    )

    try:
        resp = await _request_with_reauth(hass, entry_id, "POST", url, json=payload, timeout=15)
        result = resp.json()
        user_id = result.get("Id")

        if not user_id:
            _LOGGER.error("%s: User creation returned no ID: %s", entry_id, result)
            return {"success": False, "error": "User creation failed - no ID returned"}

        _LOGGER.info(
            "%s: Created temp user '%s %s' (ID: %d) for %d door(s) valid: %s to %s",
            entry_id, first_name, last_name, user_id, len(door_ids),
            hartmann_start or "now", hartmann_end or "forever"
        )

    except httpx.HTTPStatusError as e:
        error_body = ""
        try:
            error_body = e.response.text
        except Exception:
            pass
        _LOGGER.error("%s: Error creating temp user: %s - Response: %s", entry_id, e, error_body)
        return {"success": False, "error": f"Failed to create user: {e} - {error_body}"}
    except Exception as e:
        _LOGGER.error("%s: Error creating temp user: %s", entry_id, e)
        return {"success": False, "error": f"Failed to create user: {e}"}

    # Add PIN credential to the user FIRST. If Hartmann rejects the PIN
    # (e.g., it's already in use by another user), roll back the user and
    # bail out before touching APGs.
    cred_url = f"{cfg['base_url']}/api/Users/{user_id}/Credentials"
    cred_payload = {
        "Name": f"PIN-{code_name}",
        "CredentialType": "PinOnly",
        "SiteCode": 0,
        "CardNumber": 0,
        "PinNumber": int(pin_code),
    }

    try:
        await _request_with_reauth(hass, entry_id, "POST", cred_url, json=cred_payload, timeout=10)
        _LOGGER.info("%s: Added PIN credential to user %d", entry_id, user_id)
    except httpx.HTTPStatusError as e:
        # Try to extract actual error message from Hartmann
        error_detail = str(e)
        try:
            error_body = e.response.text
            if error_body:
                try:
                    import json as _json
                    error_json = _json.loads(error_body)
                    if isinstance(error_json, dict):
                        error_detail = (
                            error_json.get("Message")
                            or error_json.get("message")
                            or error_json.get("error")
                            or error_json.get("Error")
                            or error_json.get("ResponseStatus", {}).get("Message")
                            or error_body
                        )
                except _json.JSONDecodeError:
                    error_detail = error_body if len(error_body) < 500 else error_body[:500]
        except Exception:
            pass

        _LOGGER.error("%s: Error adding credential to user %d: %s", entry_id, user_id, error_detail)
        # Roll back the user we just created
        try:
            delete_url = f"{cfg['base_url']}/api/Users/{user_id}"
            await _request_with_reauth(hass, entry_id, "DELETE", delete_url, timeout=10)
        except Exception:
            pass
        return {"success": False, "error": f"PIN rejected: {error_detail}"}

    except Exception as e:
        _LOGGER.error("%s: Error adding credential to user %d: %s", entry_id, user_id, e)
        try:
            delete_url = f"{cfg['base_url']}/api/Users/{user_id}"
            await _request_with_reauth(hass, entry_id, "DELETE", delete_url, timeout=10)
        except Exception:
            pass
        return {"success": False, "error": f"Failed to add PIN credential: {e}"}

    # Assign the user to each door's APG. Per-door failures don't roll back the
    # whole operation — we report which doors got assigned and which didn't.
    door_results: list[dict] = []
    for did in door_ids:
        did_int = int(did)
        apg_id = apg_by_door.get(did_int)
        if not apg_id:
            door_results.append({
                "door_id": did_int,
                "success": False,
                "error": "Could not find/create Access Privilege Group for door",
            })
            continue

        apg_user_url = f"{cfg['base_url']}/api/AccessPrivilegeGroups/{apg_id}/Users/{user_id}"
        try:
            await _request_with_reauth(hass, entry_id, "PUT", apg_user_url, json={}, timeout=10)
            _LOGGER.info("%s: Assigned user %d to APG %d (door %d)", entry_id, user_id, apg_id, did_int)
            door_results.append({"door_id": did_int, "success": True})
        except Exception as e:
            _LOGGER.warning(
                "%s: Failed to assign user %d to APG %d (door %d): %s",
                entry_id, user_id, apg_id, did_int, e
            )
            door_results.append({"door_id": did_int, "success": False, "error": str(e)})

    return {"success": True, "user_id": user_id, "doors": door_results}


async def delete_temp_code_user(
    hass,
    entry_id: str,
    door_id: int,
    pin_code: str,
) -> dict:
    """
    Delete a temporary user by finding them via their PIN code.
    
    Returns {"success": True} on success,
    or {"success": False, "error": str} on failure.
    """
    cfg = hass.data[DOMAIN][entry_id]
    
    # Find users with our naming convention (FirstName starts with "HA-{pin_code}")
    search_prefix = f"HA-{pin_code}"
    
    # Get all users and find the one with matching PIN
    users = await get_partition_users(hass, entry_id)
    target_user = None
    
    for user in users:
        first_name = user.get("FirstName", "")
        if first_name == search_prefix:
            target_user = user
            break
        
        # Also check credentials if name doesn't match directly
        user_id = user.get("Id")
        if user_id and first_name.startswith("HA-"):
            creds = await get_user_credentials(hass, entry_id, user_id)
            for cred in creds:
                if str(cred.get("PinNumber")) == str(pin_code):
                    target_user = user
                    break
            if target_user:
                break
    
    if not target_user:
        _LOGGER.debug("%s: No temp user found with PIN %s", entry_id, pin_code)
        return {"success": False, "error": f"No temporary user found with PIN {pin_code}"}
    
    user_id = target_user.get("Id")
    
    # Delete the user
    url = f"{cfg['base_url']}/api/Users/{user_id}?forceDelete=true"
    
    try:
        await _request_with_reauth(hass, entry_id, "DELETE", url, timeout=10)
        _LOGGER.info("%s: Deleted temp user %d (PIN: %s)", entry_id, user_id, pin_code)
        return {"success": True}

    except Exception as e:
        # Hartmann sometimes returns 400 from DELETE even when the user record
        # was successfully removed — typically due to audit-history references
        # the API can't fully clean up under forceDelete. Verify by re-fetching:
        # if the user is gone, treat the failed call as success.
        try:
            verify_url = f"{cfg['base_url']}/api/Users/{user_id}"
            verify_resp = await _request_with_reauth(
                hass, entry_id, "GET", verify_url, timeout=10
            )
            # If we got a 200, the user still exists → real failure
            still_exists = bool(verify_resp.json())
        except httpx.HTTPStatusError as verify_err:
            # 404 means the user is gone → treat as success
            if verify_err.response is not None and verify_err.response.status_code == 404:
                still_exists = False
            else:
                still_exists = True
        except Exception:
            still_exists = True

        if not still_exists:
            _LOGGER.info(
                "%s: DELETE for user %d returned an error but user is gone — treating as success",
                entry_id, user_id,
            )
            return {"success": True}

        _LOGGER.error("%s: Error deleting temp user %d: %s", entry_id, user_id, e)
        return {"success": False, "error": f"Failed to delete user: {e}"}


async def update_temp_code_user(
    hass,
    entry_id: str,
    user_id: int,
    end_time: Optional[str] = None,
    start_time: Optional[str] = None,
) -> dict:
    """
    Update a temp code user's StartedOn/ExpiresOn via PATCH-style PUT.
    Uses Hartmann's GenericUpdateRequest format:
      {"Properties": [{"Name": "ExpiresOn", "Value": "..."}]}
    """
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/Users/{user_id}"
    
    properties = []
    if end_time is not None:
        hartmann_end = _convert_datetime_for_hartmann(end_time, hass)
        if hartmann_end:
            properties.append({"Name": "ExpiresOn", "Value": hartmann_end})
    if start_time is not None:
        hartmann_start = _convert_datetime_for_hartmann(start_time, hass)
        if hartmann_start:
            properties.append({"Name": "StartedOn", "Value": hartmann_start})
    
    if not properties:
        return {"success": False, "error": "No updates to apply"}
    
    payload = {"Properties": properties}
    
    try:
        await _request_with_reauth(hass, entry_id, "PUT", url, json=payload, timeout=10)
        _LOGGER.info("%s: Updated temp user %d: %s", entry_id, user_id, 
                     {p["Name"]: p["Value"] for p in properties})
        return {"success": True, "user_id": user_id}
    except Exception as e:
        _LOGGER.error("%s: Error updating temp user %d: %s", entry_id, user_id, e)
        return {"success": False, "error": f"Failed to update user: {e}"}


async def update_panels(hass, entry_id: str) -> dict:
    """Send 'Update Panels' command to push config to all connected panels."""
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/PanelCommands/UpdateAll"
    try:
        await _request_with_reauth(hass, entry_id, "POST", url, json={}, timeout=15)
        _LOGGER.info("%s: Update Panels command sent", entry_id)
        return {"success": True}
    except Exception as e:
        _LOGGER.error("%s: Error sending Update Panels: %s", entry_id, e)
        return {"success": False, "error": f"Failed to update panels: {e}"}


async def add_user_to_door_apg(
    hass,
    entry_id: str,
    user_id: int,
    door_id: int,
) -> dict:
    """Assign an existing temp user to an additional door's APG.

    Used by the `add_door_to_temp_code` service to extend an existing temp
    code's reach to a new door without changing the PIN. Finds (or creates,
    if it doesn't exist yet) the `HA Temp Access - {door_name}` APG for the
    given door and PUTs the user into it.

    Returns {"success": True, "apg_id": int} on success,
    or {"success": False, "error": str} on failure.
    """
    cfg = hass.data[DOMAIN][entry_id]

    # Resolve door name
    doors = await get_all_doors(hass, entry_id)
    door_name: Optional[str] = None
    for d in doors:
        if int(d.get("Id") or 0) == int(door_id):
            door_name = d.get("Name") or f"Door {door_id}"
            break
    if not door_name:
        return {"success": False, "error": f"Door {door_id} not found"}

    apg_id = await find_or_create_temp_apg(hass, entry_id, int(door_id), door_name)
    if not apg_id:
        return {"success": False, "error": "Failed to create/find Access Privilege Group for door"}

    apg_user_url = f"{cfg['base_url']}/api/AccessPrivilegeGroups/{apg_id}/Users/{user_id}"
    try:
        await _request_with_reauth(hass, entry_id, "PUT", apg_user_url, json={}, timeout=10)
        _LOGGER.info(
            "%s: Assigned user %d to APG %d (door %d / %s)",
            entry_id, user_id, apg_id, door_id, door_name,
        )
        return {"success": True, "apg_id": apg_id}
    except Exception as e:
        _LOGGER.error(
            "%s: Failed to assign user %d to APG %d (door %d): %s",
            entry_id, user_id, apg_id, door_id, e,
        )
        return {"success": False, "error": str(e)}


async def remove_user_from_door_apg(
    hass,
    entry_id: str,
    user_id: int,
    door_id: int,
) -> dict:
    """Remove a temp user from a single door's APG.

    Used by the `remove_door_from_temp_code` service. Looks up the APG by
    its `HA Temp Access - {door_name}` name and DELETEs the user from it.
    The Hartmann user record is left intact; this only removes one APG
    assignment.

    Returns {"success": True} on success,
    or {"success": False, "error": str} on failure.
    """
    cfg = hass.data[DOMAIN][entry_id]

    # Resolve door name to find the APG
    doors = await get_all_doors(hass, entry_id)
    door_name: Optional[str] = None
    for d in doors:
        if int(d.get("Id") or 0) == int(door_id):
            door_name = d.get("Name") or f"Door {door_id}"
            break
    if not door_name:
        return {"success": False, "error": f"Door {door_id} not found"}

    apg_name = f"HA Temp Access - {door_name}"
    existing_apgs = await get_access_privilege_groups(hass, entry_id)
    apg_id: Optional[int] = None
    for apg in existing_apgs:
        if apg.get("Name") == apg_name:
            apg_id = apg.get("Id")
            break

    if not apg_id:
        # Already not assigned — treat as success
        _LOGGER.info(
            "%s: APG '%s' does not exist; nothing to remove for user %d",
            entry_id, apg_name, user_id,
        )
        return {"success": True, "note": "APG did not exist"}

    apg_user_url = f"{cfg['base_url']}/api/AccessPrivilegeGroups/{apg_id}/Users/{user_id}"
    try:
        await _request_with_reauth(hass, entry_id, "DELETE", apg_user_url, timeout=10)
        _LOGGER.info(
            "%s: Removed user %d from APG %d (door %d / %s)",
            entry_id, user_id, apg_id, door_id, door_name,
        )
        return {"success": True, "apg_id": apg_id}
    except httpx.HTTPStatusError as e:
        # 404 = not in that APG → idempotent success
        if e.response is not None and e.response.status_code == 404:
            _LOGGER.info(
                "%s: User %d was not in APG %d (door %d) — treating as success",
                entry_id, user_id, apg_id, door_id,
            )
            return {"success": True, "note": "User was not assigned to this APG"}
        _LOGGER.error(
            "%s: Failed to remove user %d from APG %d (door %d): %s",
            entry_id, user_id, apg_id, door_id, e,
        )
        return {"success": False, "error": str(e)}
    except Exception as e:
        _LOGGER.error(
            "%s: Failed to remove user %d from APG %d (door %d): %s",
            entry_id, user_id, apg_id, door_id, e,
        )
        return {"success": False, "error": str(e)}


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


# ─────────────────────────────────────────────────────────────────────────────
# OTR (One Time Run) schedule functions
# ─────────────────────────────────────────────────────────────────────────────

# Valid modes for OneTimeRun door overrides
ONE_TIME_RUN_MODES = [
    "Lockdown",
    "Card",
    "Pin", 
    "CardOrPin",
    "CardAndPin",
    "Unlock",
    "UnlockWithFirstCardIn",
    "DualCard",
]


async def create_one_time_run(
    hass,
    entry_id: str,
    door_ids: list[int],
    start_time: str,
    stop_time: str,
    mode: str = "Unlock",
    name: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    """
    Create an OTR schedule for doors.
    
    Args:
        door_ids: List of door IDs to apply the schedule to
        start_time: ISO datetime string for when override starts
        stop_time: ISO datetime string for when override ends
        mode: Door mode during override (Unlock, Lockdown, Card, etc.)
        name: Optional name for the schedule
        description: Optional description
    
    Returns:
        {"success": True, "id": schedule_id} on success
        {"success": False, "error": str} on failure
    """
    cfg = hass.data[DOMAIN][entry_id]
    
    # Validate mode
    if mode not in ONE_TIME_RUN_MODES:
        return {"success": False, "error": f"Invalid mode '{mode}'. Must be one of: {ONE_TIME_RUN_MODES}"}
    
    # Convert datetimes to Hartmann format (UTC)
    hartmann_start = _convert_datetime_for_hartmann(start_time, hass)
    hartmann_stop = _convert_datetime_for_hartmann(stop_time, hass)
    
    if not hartmann_start or not hartmann_stop:
        return {"success": False, "error": "Invalid start_time or stop_time format"}
    
    # Build door selections
    doors = [{"Id": door_id, "Mode": mode} for door_id in door_ids]
    
    # Generate name if not provided
    if not name:
        from datetime import datetime
        name = f"HA Schedule {datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    # Build payload - include BOTH formats for cross-compatibility:
    # Protector.Net uses top-level StartTime/StopTime
    # Odyssey uses Dates array of DatePair objects
    payload = {
        "Name": name[:60],  # Max 60 chars
        "StartTime": hartmann_start,
        "StopTime": hartmann_stop,
        "Doors": doors,
        "Dates": [{"StartTime": hartmann_start, "StopTime": hartmann_stop}],
    }
    
    if description:
        payload["Description"] = description[:255]  # Max 255 chars
    
    url = f"{cfg['base_url']}/api/OneTimeRunTimeZones/Doors"
    
    _LOGGER.debug("%s: Creating OneTimeRun schedule: %s", entry_id, payload)
    
    try:
        resp = await _request_with_reauth(hass, entry_id, "POST", url, json=payload, timeout=15)
        result = resp.json()
        schedule_id = result.get("Id")
        
        _LOGGER.debug("%s: OneTimeRun POST response: %s", entry_id, result)
        
        # Hartmann often returns Id: 0 even on success - try to find the real ID
        if schedule_id == 0:
            _LOGGER.debug("%s: Hartmann returned Id: 0, fetching list to find real ID", entry_id)
            # Wait a moment for Hartmann to process
            import asyncio
            await asyncio.sleep(0.5)
            
            # Fetch all schedules and find ours by name
            schedules = await get_one_time_runs(hass, entry_id, door_id=None)
            for sched in schedules:
                if sched.get("name") == name:
                    schedule_id = sched.get("id")
                    _LOGGER.info("%s: Found real schedule ID %s for '%s'", entry_id, schedule_id, name)
                    break
        
        # Check for None specifically - ID of 0 might still be valid if we couldn't find it
        if schedule_id is not None:
            _LOGGER.info("%s: Created OneTimeRun schedule ID %s: %s to %s", 
                        entry_id, schedule_id, hartmann_start, hartmann_stop)
            return {
                "success": True, 
                "id": schedule_id, 
                "name": name,
                "start_time": _convert_datetime_from_hartmann(hartmann_start, hass),
                "stop_time": _convert_datetime_from_hartmann(hartmann_stop, hass),
                "mode": mode,
                "door_ids": door_ids,
            }
        else:
            _LOGGER.error("%s: OneTimeRun creation returned no ID: %s", entry_id, result)
            return {"success": False, "error": "Schedule creation failed - no ID returned"}
            
    except httpx.HTTPStatusError as e:
        error_body = ""
        try:
            error_body = e.response.text
        except Exception:
            pass
        _LOGGER.error("%s: Error creating OneTimeRun: %s - %s", entry_id, e, error_body)
        return {"success": False, "error": f"Failed to create schedule: {e} - {error_body}"}
    except Exception as e:
        _LOGGER.error("%s: Error creating OneTimeRun: %s", entry_id, e)
        return {"success": False, "error": f"Failed to create schedule: {e}"}


async def get_one_time_runs(
    hass,
    entry_id: str,
    door_id: Optional[int] = None,
) -> list[dict]:
    """
    Get list of OTR schedules.
    
    Args:
        door_id: Optional filter by door ID
    
    Returns:
        List of schedule dicts with id, name, start_time, stop_time, door_name, mode, door_ids
    """
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/OneTimeRunTimeZones/Doors"
    params = {"PageNumber": 1, "PerPage": 100}
    
    try:
        resp = await _request_with_reauth(hass, entry_id, "GET", url, params=params, timeout=15)
        data = resp.json()
        results = data.get("Results", [])
        
        _LOGGER.debug("%s: Raw OTR API response: %s", entry_id, results[:2] if results else "empty")
        
        # Build a DoorName -> DoorId lookup from the integration's cached door data.
        # The list endpoint (DoorOneTimeRunViewModel) returns DoorName but NOT DoorId,
        # so we need to resolve names to IDs ourselves.
        door_name_to_id: dict[str, int] = {}
        try:
            all_doors = await get_all_doors(hass, entry_id)
            for d in all_doors:
                dname = d.get("Name", "")
                did = d.get("Id")
                if dname and did is not None:
                    door_name_to_id[dname] = did
            _LOGGER.debug("%s: Built door name→ID map with %d doors", entry_id, len(door_name_to_id))
        except Exception as map_err:
            _LOGGER.warning("%s: Could not build door name→ID map: %s", entry_id, map_err)
        
        schedules = []
        for r in results:
            # The list endpoint returns DoorName (string) per entry, NOT a Doors array.
            # Each OTR entry in Hartmann is per-door, so we resolve the ID from DoorName.
            door_ids = []
            
            # Try DoorId field first (in case Hartmann adds it in the future)
            if r.get("DoorId"):
                door_ids = [r.get("DoorId")]
            
            # Try Doors array (in case Hartmann adds it)
            if not door_ids:
                doors_arr = r.get("Doors", [])
                door_ids = [d.get("Id") for d in doors_arr if d.get("Id") is not None]
            
            # Resolve DoorName to DoorId using our lookup
            if not door_ids and r.get("DoorName") and r.get("DoorName") in door_name_to_id:
                door_ids = [door_name_to_id[r["DoorName"]]]
                _LOGGER.debug("%s: Resolved DoorName '%s' -> DoorId %d for OTR %s",
                             entry_id, r["DoorName"], door_ids[0], r.get("Id"))
            
            if not door_ids:
                _LOGGER.debug("%s: OTR %s has no resolvable door_ids (DoorName=%s)",
                             entry_id, r.get("Id"), r.get("DoorName"))
            
            schedule = {
                "id": r.get("Id"),
                "name": r.get("Name"),
                "description": r.get("Description"),
                "start_time": _convert_datetime_from_hartmann(r.get("StartTime"), hass),
                "stop_time": _convert_datetime_from_hartmann(r.get("StopTime"), hass),
                "door_name": r.get("DoorName"),
                "site_name": r.get("SiteName"),
                "mode": r.get("Mode"),
                "partition_id": r.get("PartitionId"),
                "door_ids": door_ids,
            }
            
            # Filter by door_id if provided
            if door_id is not None:
                if door_ids and door_id not in door_ids:
                    continue
                # If door_ids is empty, fall back to matching by door_name
                if not door_ids:
                    # Look up the name of the door we're filtering for
                    target_door_name = None
                    for dn, did in door_name_to_id.items():
                        if did == door_id:
                            target_door_name = dn
                            break
                    # Only include if door_name matches; skip unknown entries
                    if not target_door_name or r.get("DoorName") != target_door_name:
                        continue
            
            schedules.append(schedule)
        
        _LOGGER.debug("%s: Found %d OneTimeRun schedules (filter door_id=%s)", entry_id, len(schedules), door_id)
        return schedules
        
    except Exception as e:
        if _is_transient_outage(e):
            _LOGGER.info(
                "%s: OneTimeRun fetch failed (server unreachable, will retry): %s",
                entry_id, e,
            )
        else:
            _LOGGER.warning("%s: Error fetching OneTimeRun schedules: %s", entry_id, e)
        return []


async def delete_one_time_run(
    hass,
    entry_id: str,
    schedule_id: int,
) -> dict:
    """
    Delete an OTR schedule.
    
    Args:
        schedule_id: The ID of the schedule to delete
    
    Returns:
        {"success": True} on success
        {"success": False, "error": str} on failure
    """
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/OneTimeRunTimeZones/Doors/{schedule_id}"
    
    try:
        await _request_with_reauth(hass, entry_id, "DELETE", url, timeout=10)
        _LOGGER.info("%s: Deleted OneTimeRun schedule ID %d", entry_id, schedule_id)
        return {"success": True, "id": schedule_id}
        
    except httpx.HTTPStatusError as e:
        error_body = ""
        try:
            error_body = e.response.text
        except Exception:
            pass
        _LOGGER.error("%s: Error deleting OneTimeRun %d: %s - %s", entry_id, schedule_id, e, error_body)
        return {"success": False, "error": f"Failed to delete schedule: {e} - {error_body}"}
    except Exception as e:
        _LOGGER.error("%s: Error deleting OneTimeRun %d: %s", entry_id, schedule_id, e)
        return {"success": False, "error": f"Failed to delete schedule: {e}"}



# ============================================================================
# Managed Door Schedules (HA-controlled DoorTimeZones)
# ============================================================================
#
# Goal: let HA flip a door's "default" schedule (DoorTimeZoneId) at runtime so
# changes survive panel reboots — unlike PanelCommands/OverrideDoor which
# does not persist across panel restarts.
#
# Verified API shapes (Protector.Net + Odyssey share these):
#
#   GET  /api/Doors                           -> Paged list of full Door
#                                                objects (HAS DoorTimeZoneId).
#                                                The single-door GET returns
#                                                DoorBase wrapped in {Result},
#                                                which LACKS DoorTimeZoneId,
#                                                so we never use it.
#
#   PUT  /api/Doors/{Id}                      -> GenericUpdateRequest envelope:
#                                                {"Properties":[{"Name":..,"Value":..}]}
#                                                Just the fields you want to
#                                                change. NOT whole-object.
#
#   POST /api/DoorTimeZones                   -> AddDoorTimeZone:
#                                                {"PartitionIds":[..], "Name":..,
#                                                 "Description":..,
#                                                 "TimeSpans":[DoorTimeSpanRequest]}
#
#   PUT  /api/DoorTimeZones/{Id}/TimeSpans    -> UpdateDoorTimeSpans:
#                                                {"Id":.., "TimeSpans":[..]}
#
#   DoorTimeSpanRequest fields:
#     DayOfWeek  : enum string ("Sunday".."Saturday")
#     StartTime  : int (minutes from midnight, 0..1439)
#     StopTime   : int (minutes from midnight, 0..1439)
#     Mode       : enum string (DoorTimeZoneMode)


# Day-of-week index -> enum string.
_DOW_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

# Mirrored from the swagger DoorTimeZoneMode enum so we can validate locally.
DoorTimeZoneMode_VALUES = {
    "Lockdown", "Card", "Pin", "CardOrPin", "CardAndPin",
    "Unlock", "UnlockWithFirstCardIn", "DualCard",
}


def _ha_tz_description(entry_id: str, door_id: int) -> str:
    """Description tag we stamp on every HA-created DoorTimeZone.

    Used during cleanup to identify HA-created TZs unambiguously, even if a
    user manually renames them in the Hartmann UI.
    """
    return f"protector_net:{entry_id}:door:{door_id}"


def _ha_tz_name(entry_id: str, door_name: str) -> str:
    """Visible name shown in the Hartmann TimeZones list. Max 60 chars."""
    short = entry_id[:8]
    full = f"HA[{short}] {door_name}"
    return full[:60]


def _build_full_week_timespans(mode: str) -> list[dict]:
    """7 spans, one per weekday, 12:00 AM (0) -> 11:59 PM (1439), all in `mode`."""
    if mode not in DoorTimeZoneMode_VALUES:
        raise ValueError(f"Unknown DoorTimeZoneMode: {mode}")
    return [
        {"DayOfWeek": _DOW_NAMES[d], "StartTime": 0, "StopTime": 1439, "Mode": mode}
        for d in range(7)
    ]


# ---------------------------------------------------------------------------
# Door — read DoorTimeZoneId, change DoorTimeZoneId
# ---------------------------------------------------------------------------

async def get_door_with_tz(hass, entry_id: str, door_id: int) -> Optional[dict]:
    """Find a door (full object including DoorTimeZoneId) by Id.

    We use the LIST endpoint because the single-door GET returns DoorBase
    (no DoorTimeZoneId field). The list endpoint returns the full Door schema.
    """
    for d in await get_all_doors(hass, entry_id):
        try:
            if int(d.get("Id") or 0) == int(door_id):
                return d
        except (TypeError, ValueError):
            continue
    return None


async def get_door_time_zone_id(hass, entry_id: str, door_id: int) -> Optional[int]:
    """Return the DoorTimeZoneId for a given door, or None if not findable."""
    door = await get_door_with_tz(hass, entry_id, door_id)
    if not door:
        return None
    tz = door.get("DoorTimeZoneId")
    try:
        return int(tz) if tz is not None else None
    except (TypeError, ValueError):
        return None


async def set_door_time_zone_id(
    hass,
    entry_id: str,
    door_id: int,
    new_tz_id: int,
) -> bool:
    """PUT a single-property change to /api/Doors/{Id}.

    Body shape (GenericUpdateRequest):
        {"Properties": [{"Name": "DoorTimeZoneId", "Value": <int>}]}

    Idempotent at the caller level: managed_schedules checks current_mode
    and skips no-op flips before reaching here.
    """
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/Doors/{door_id}"
    payload = {"Properties": [{"Name": "DoorTimeZoneId", "Value": int(new_tz_id)}]}
    try:
        await _request_with_reauth(hass, entry_id, "PUT", url, json=payload, timeout=15)
        _LOGGER.info(
            "%s: Door %s DoorTimeZoneId -> %s", entry_id, door_id, new_tz_id,
        )
        return True
    except httpx.HTTPStatusError as e:
        body = ""
        try:
            body = e.response.text
        except Exception:
            pass
        _LOGGER.error(
            "%s: Door %s PUT failed (set DoorTimeZoneId=%s): %s - %s",
            entry_id, door_id, new_tz_id, e, body,
        )
        return False
    except Exception as e:
        _LOGGER.error(
            "%s: Door %s PUT failed (set DoorTimeZoneId=%s): %s",
            entry_id, door_id, new_tz_id, e,
        )
        return False


# ---------------------------------------------------------------------------
# DoorTimeZone CRUD
# ---------------------------------------------------------------------------

async def list_door_time_zones(hass, entry_id: str) -> list[dict]:
    """GET /api/DoorTimeZones - list TZs in the partition."""
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/DoorTimeZones"
    params = {"PartitionId": cfg["partition_id"], "PageNumber": 1, "PerPage": 500}
    try:
        resp = await _request_with_reauth(hass, entry_id, "GET", url, params=params, timeout=10)
        data = resp.json() or {}
        if isinstance(data, list):
            return data
        return data.get("Results") or []
    except Exception as e:
        _LOGGER.warning("%s: Failed to list DoorTimeZones: %s", entry_id, e)
        return []


async def _create_door_time_zone(
    hass,
    entry_id: str,
    name: str,
    description: str,
    time_spans: list[dict],
) -> Optional[int]:
    """POST /api/DoorTimeZones (AddDoorTimeZone shape).

    Body:
        {
          "PartitionIds": [<partition_id>],
          "Name": "...",
          "Description": "...",
          "TimeSpans": [DoorTimeSpanRequest, ...]
        }

    Returns new TZ Id on success, or None on failure. Handles Id=0 by
    re-listing and matching on Name + Description tag.
    """
    cfg = hass.data[DOMAIN][entry_id]
    payload = {
        "PartitionIds": [int(cfg["partition_id"])],
        "Name":         name[:60],
        "Description":  description[:255],
        "TimeSpans":    time_spans,
    }
    url = f"{cfg['base_url']}/api/DoorTimeZones"
    _LOGGER.debug("%s: POST DoorTimeZone: %s", entry_id, payload)

    try:
        resp = await _request_with_reauth(hass, entry_id, "POST", url, json=payload, timeout=15)
        result = resp.json() if resp.content else {}
        tz_id = result.get("Id") if isinstance(result, dict) else None

        if not tz_id:
            # Hartmann's "Id=0 means success but you have to refetch" quirk.
            import asyncio
            await asyncio.sleep(0.5)
            for tz in await list_door_time_zones(hass, entry_id):
                if str(tz.get("Name") or "") == name[:60] \
                   and str(tz.get("Description") or "") == description[:255]:
                    tz_id = tz.get("Id")
                    _LOGGER.debug("%s: Resolved DoorTimeZone Id=%s via re-list", entry_id, tz_id)
                    break

        if tz_id:
            _LOGGER.info("%s: Created DoorTimeZone Id=%s name=%r", entry_id, tz_id, name)
            return int(tz_id)
        _LOGGER.error("%s: DoorTimeZone POST returned no Id: %s", entry_id, result)
        return None
    except httpx.HTTPStatusError as e:
        body = ""
        try:
            body = e.response.text
        except Exception:
            pass
        _LOGGER.error("%s: DoorTimeZone POST failed: %s - %s", entry_id, e, body)
        return None
    except Exception as e:
        _LOGGER.error("%s: DoorTimeZone POST failed: %s", entry_id, e)
        return None


async def _put_door_time_zone_time_spans(
    hass,
    entry_id: str,
    tz_id: int,
    time_spans: list[dict],
) -> bool:
    """PUT /api/DoorTimeZones/{Id}/TimeSpans (UpdateDoorTimeSpans shape).

    Body:
        {"Id": <tz_id>, "TimeSpans": [DoorTimeSpanRequest, ...]}
    """
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/DoorTimeZones/{tz_id}/TimeSpans"
    payload = {"Id": int(tz_id), "TimeSpans": time_spans}
    try:
        await _request_with_reauth(hass, entry_id, "PUT", url, json=payload, timeout=15)
        return True
    except httpx.HTTPStatusError as e:
        body = ""
        try:
            body = e.response.text
        except Exception:
            pass
        _LOGGER.error(
            "%s: TimeSpans PUT failed for tz %s: %s - %s",
            entry_id, tz_id, e, body,
        )
        return False
    except Exception as e:
        _LOGGER.error("%s: TimeSpans PUT failed for tz %s: %s", entry_id, tz_id, e)
        return False


async def _delete_door_time_zone(hass, entry_id: str, tz_id: int) -> bool:
    """DELETE /api/DoorTimeZones/{Id}.

    Hartmann normally refuses to delete a TZ that's still assigned to a door,
    so callers must repoint any doors at their original TZ first.
    """
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/DoorTimeZones/{tz_id}"
    try:
        await _request_with_reauth(hass, entry_id, "DELETE", url, timeout=10)
        _LOGGER.info("%s: Deleted DoorTimeZone Id=%s", entry_id, tz_id)
        return True
    except httpx.HTTPStatusError as e:
        body = ""
        try:
            body = e.response.text
        except Exception:
            pass
        _LOGGER.warning("%s: DELETE DoorTimeZone %s failed: %s - %s", entry_id, tz_id, e, body)
        return False
    except Exception as e:
        _LOGGER.warning("%s: DELETE DoorTimeZone %s failed: %s", entry_id, tz_id, e)
        return False


# ---------------------------------------------------------------------------
# High-level helpers used by managed_schedules.py
# ---------------------------------------------------------------------------

async def provision_managed_tz(
    hass,
    entry_id: str,
    door_id: int,
    door_name: str,
    initial_mode: str,
) -> Optional[dict]:
    """Create the HA DoorTimeZone for a given door.

    Returns {"id": int, "name": str, "mode": str} on success, None on failure.
    """
    name = _ha_tz_name(entry_id, door_name)
    desc = _ha_tz_description(entry_id, door_id)
    spans = _build_full_week_timespans(initial_mode)
    tz_id = await _create_door_time_zone(hass, entry_id, name, desc, spans)
    if not tz_id:
        return None
    return {"id": tz_id, "name": name, "mode": initial_mode}


async def set_managed_tz_mode(
    hass,
    entry_id: str,
    tz_id: int,
    name: str,        # accepted for symmetry; not needed by /TimeSpans endpoint
    door_id: int,     # ditto
    mode: str,
) -> bool:
    """Replace the HA TZ's TimeSpans with 7 days at the given mode."""
    spans = _build_full_week_timespans(mode)
    return await _put_door_time_zone_time_spans(hass, entry_id, tz_id, spans)


async def find_orphan_managed_tzs(hass, entry_id: str) -> list[dict]:
    """Find DoorTimeZones in the partition tagged for THIS entry.

    Used by recovery / cleanup paths to catch HA TZs that ended up orphaned
    (e.g. options state corrupted, integration deleted then re-added).
    """
    tag = f"protector_net:{entry_id}:"
    out = []
    for tz in await list_door_time_zones(hass, entry_id):
        desc = str(tz.get("Description") or "")
        if desc.startswith(tag):
            out.append(tz)
    return out


# ============================================================================
# Panel status (used by the hub-device panels-online sensor)
# ============================================================================

async def get_panels_online(hass, entry_id: str) -> dict:
    """GET /api/PanelCommands/PanelsOnline.

    Returns:
        {"online": ["<mac>",...], "offline": ["<mac>",...]}
    or {"online": [], "offline": []} on error (logged at debug level).
    """
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/PanelCommands/PanelsOnline"
    try:
        resp = await _request_with_reauth(hass, entry_id, "GET", url, timeout=10)
        data = resp.json() or {}
        return {
            "online":  list(data.get("PanelsOnline") or []),
            "offline": list(data.get("PanelsOffline") or []),
        }
    except Exception as e:
        _LOGGER.debug("%s: Failed to fetch PanelsOnline: %s", entry_id, e)
        return {"online": [], "offline": []}


async def get_panels(hass, entry_id: str) -> list[dict]:
    """GET /api/Panels (partition-scoped) — full HardwarePanel list.

    Used to build a MAC -> Name (and model) map for the panels-online
    sensor so its attributes show friendly identifiers, not raw MACs.
    """
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/Panels"
    params = {"PartitionId": cfg["partition_id"], "PageNumber": 1, "PerPage": 500}
    try:
        resp = await _request_with_reauth(hass, entry_id, "GET", url, params=params, timeout=10)
        data = resp.json() or {}
        if isinstance(data, list):
            return data
        return data.get("Results") or []
    except Exception as e:
        _LOGGER.debug("%s: Failed to list Panels: %s", entry_id, e)
        return []


# ============================================================================
# Panel inputs (used by the per-door door-contact binary_sensor)
# ============================================================================

async def get_panel_inputs(hass, entry_id: str, panel_id: int) -> list[dict]:
    """GET /api/Panels/{panel_id}/Inputs — list inputs (PanelInputConfiguration).

    Response shape on Protector.Net is a flat array; on Odyssey it's wrapped
    in {"Results": [...]}. Both shapes handled.

    Each item has at least: Id, Name, IndexOnPanel, InputUsage (string),
    InputUsageVal (int), IsInverted, DoorIndex, ParentPanelId.
    Returns [] on any error (logged at debug — not actionable for the user).
    """
    cfg = hass.data[DOMAIN][entry_id]
    url = f"{cfg['base_url']}/api/Panels/{int(panel_id)}/Inputs"
    try:
        resp = await _request_with_reauth(hass, entry_id, "GET", url, timeout=10)
        data = resp.json() or []
        if isinstance(data, list):
            return data
        return data.get("Results") or []
    except Exception as e:
        _LOGGER.debug("%s: Failed to list inputs for panel %s: %s",
                      entry_id, panel_id, e)
        return []


async def build_door_contact_map(hass, entry_id: str) -> dict[tuple[str, int], dict]:
    """Discover the (panel_mac, input_index) -> door-contact map for this entry.

    Walks every panel in the partition, lists its inputs, keeps only those
    with InputUsage in DOOR_CONTACT_USAGES, and resolves DoorIndex (the input's
    door-on-this-panel position 0-15) to the corresponding Hartmann door_id by
    matching against Door.IndexOnPanel.

    Returns:
        {(panel_mac, input_index_on_panel): {
            "door_id":     int,
            "panel_id":    int,
            "is_inverted": bool,
            "input_name":  str,
        }}

    Empty dict on any failure or if no door-contact inputs exist. Not raising:
    the binary_sensor platform falls back to "Unknown" gracefully when the
    map is empty, so a transient API error just means contacts won't update
    until the next hourly sync rebuilds the map.
    """
    from .const import DOOR_CONTACT_USAGES  # local to keep top-of-file imports tidy

    contact_map: dict[tuple[str, int], dict] = {}

    panels = await get_panels(hass, entry_id)
    if not panels:
        return contact_map

    # Doors are partition-scoped; one fetch covers all panels.
    doors = await get_all_doors(hass, entry_id)
    if not doors:
        return contact_map

    # Build (parent_panel_id, index_on_panel) -> door_id.
    door_by_panel_index: dict[tuple[int, int], int] = {}
    for d in doors:
        try:
            ppid = int(d.get("ParentPanelId"))
            iop  = int(d.get("IndexOnPanel"))
            did  = int(d.get("Id"))
        except (TypeError, ValueError):
            continue
        door_by_panel_index[(ppid, iop)] = did

    for panel in panels:
        try:
            panel_id = int(panel.get("Id"))
        except (TypeError, ValueError):
            continue
        mac = (panel.get("PanelMacAddress") or "").strip()
        if not mac:
            continue

        inputs = await get_panel_inputs(hass, entry_id, panel_id)
        for inp in inputs:
            usage = str(inp.get("InputUsage") or "")
            if usage not in DOOR_CONTACT_USAGES:
                continue
            try:
                idx = int(inp.get("IndexOnPanel"))
                door_idx = int(inp.get("DoorIndex"))
            except (TypeError, ValueError):
                continue

            door_id = door_by_panel_index.get((panel_id, door_idx))
            if door_id is None:
                # Input is configured as a contact but its DoorIndex doesn't
                # resolve to any door we know about. Skip — surfacing this
                # as an entity would be confusing.
                _LOGGER.debug(
                    "%s: Door_Contact input on panel %s idx=%s targets "
                    "DoorIndex=%s but no door has that IndexOnPanel; skipping",
                    entry_id, panel_id, idx, door_idx,
                )
                continue

            contact_map[(mac, idx)] = {
                "door_id":     door_id,
                "panel_id":    panel_id,
                "is_inverted": bool(inp.get("IsInverted")),
                "input_name":  str(inp.get("Name") or f"Input {idx}"),
            }

    if contact_map:
        _LOGGER.debug(
            "%s: Built door-contact map: %d input(s) across %d door(s)",
            entry_id, len(contact_map),
            len({v["door_id"] for v in contact_map.values()}),
        )
    return contact_map


async def build_door_held_open_thresholds(
    hass, entry_id: str
) -> dict[int, int | None]:
    """Discover the per-door held-open threshold (milliseconds) for this entry.

    Returns:
        {door_id: ms_int | None}

    Where the value is:
        * `int` — `AllowedHeldOpenTime` from /api/Doors (in ms). Used by ws.py
          to start a per-door timer that synthesizes a held-open dispatch
          when the contact has stayed open longer than this threshold.

    Why this exists: Protector.Net (legacy) does NOT emit a
    `DOOR_CONTACT_STATE | HELD_OPEN` SignalR notification — its own web UI
    derives the "Held Open" badge purely client-side from the contact's
    on-time vs. this threshold. Odyssey, by contrast, DOES emit a real
    HELD_OPEN notification, so on those panels the timer's synthesized
    dispatch is just defensive (it gets pre-empted by the real notification).

    Note on `DisableHeldOpen`:
        Hartmann's door config has a `DisableHeldOpen` flag, but observed
        behavior on Protector.Net is that the WEB UI still shows the
        "Held Open" badge for doors with that flag set — it appears to
        only suppress the buzzer/audit alarm, not the visual state. We
        therefore intentionally do NOT special-case that flag here; if
        AllowedHeldOpenTime is > 0 we honor it. Users who genuinely don't
        want held-open events in HA can disable the binary_sensor's
        held_open attribute via templates, or set AllowedHeldOpenTime to
        0 in the door config.

    Empty dict on any failure — the binary_sensor's `held_open` attribute
    just stays False until the next hourly sync rebuilds the cache. Not
    raising: a missing threshold is far less damaging than blocking entity
    setup.
    """
    out: dict[int, int] = {}
    doors = await get_all_doors(hass, entry_id)
    if not doors:
        return out
    for d in doors:
        try:
            did = int(d.get("Id"))
        except (TypeError, ValueError):
            continue
        t = d.get("AllowedHeldOpenTime")
        # Hartmann sometimes returns the value as a string (varies by panel
        # firmware / API version). Tolerate both.
        if isinstance(t, bool):
            # Defensive: bool is an int subclass in Python; treat it as
            # "missing" rather than 0 or 1 ms (which would be nonsensical).
            continue
        try:
            t_int = int(t)
        except (TypeError, ValueError):
            continue
        if t_int > 0:
            out[did] = t_int
    if out:
        _LOGGER.debug(
            "%s: Built door-held-open thresholds: %d door(s)",
            entry_id, len(out),
        )
    return out

# custom_components/protector_net/api.py

import httpx
import logging
import asyncio

_LOGGER = logging.getLogger(__name__)
DOMAIN = "protector_net"


async def login(hass, base_url: str, username: str, password: str) -> str:
    """
    POST to /auth and return the ss-id session cookie.
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


async def _request_with_reauth(hass, method: str, url: str, **kwargs) -> httpx.Response:
    """
    Internal helper: send request with ss-id cookie; on 401, re-login and retry once.
    """
    # build headers
    session = hass.data[DOMAIN]["session_cookie"]
    headers = kwargs.pop("headers", {})
    headers["Content-Type"] = "application/json"
    headers["Cookie"]       = f"ss-id={session}"

    async with httpx.AsyncClient(verify=False) as client:
        resp = await client.request(method, url, headers=headers, **kwargs)
        if resp.status_code != 401:
            resp.raise_for_status()
            return resp

        # 401 → refresh
        _LOGGER.warning("Session expired, re-authenticating")
        base_url = hass.data[DOMAIN]["base_url"]
        user     = hass.data[DOMAIN]["username"]
        pwd      = hass.data[DOMAIN]["password"]
        new_cookie = await login(hass, base_url, user, pwd)
        hass.data[DOMAIN]["session_cookie"] = new_cookie

        headers["Cookie"] = f"ss-id={new_cookie}"
        resp = await client.request(method, url, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp


async def get_partitions(hass, base_url: str, session_cookie: str) -> list[dict]:
    """
    Used in config_flow: fetch partitions via cookie-auth.
    """
    headers = {
        "Content-Type": "application/json",
        "Cookie":       f"ss-id={session_cookie}",
    }
    params = {"PageNumber": 1, "PerPage": 500}
    url = f"{base_url}/api/Partitions/ByPrivilege/Manage_Doors"

    async with httpx.AsyncClient(verify=False) as client:
        resp = await client.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json().get("Results", [])


async def get_all_doors(hass) -> list[dict]:
    """
    Fetch only the doors for the selected partition by passing
    PartitionId as a query parameter.
    """
    base_url     = hass.data[DOMAIN]["base_url"]
    session      = hass.data[DOMAIN]["session_cookie"]
    partition_id = hass.data[DOMAIN]["partition_id"]

    url    = f"{base_url}/api/doors"
    params = {
        "PageNumber": 1,
        "PerPage":    500,
        "PartitionId": partition_id,
    }

    try:
        # _request_with_reauth will attach your ss-id cookie and auto-re-login
        resp = await _request_with_reauth(
            hass,
            "GET",
            url,
            params=params,
            timeout=10
        )
        return resp.json().get("Results", [])
    except Exception as e:
        _LOGGER.exception("Error fetching doors: %s", e)
        return []


async def pulse_unlock(hass, door_ids: list[int]) -> bool:
    """
    Pulse doors via PanelCommands/PulseDoor, re-auth on 401.
    """
    base_url = hass.data[DOMAIN]["base_url"]
    url      = f"{base_url}/api/PanelCommands/PulseDoor"
    payload  = {"DoorIds": door_ids}

    try:
        await _request_with_reauth(
            hass, "POST", url, json=payload, timeout=10
        )
        _LOGGER.info("Pulse unlock sent for doors: %s", door_ids)
        return True
    except Exception as e:
        _LOGGER.error("Error in pulse_unlock: %s", e)
        return False


async def set_override(hass, door_ids: list[int], override_type: str, minutes: int = None) -> bool:
    """
    Override doors via PanelCommands/OverrideDoor.
    """
    base_url      = hass.data[DOMAIN]["base_url"]
    url           = f"{base_url}/api/PanelCommands/OverrideDoor"
    override_mins = minutes or hass.data[DOMAIN]["override_minutes"]

    payload = {
        "OverrideType": override_type,
        "DoorIds":      door_ids,
        "TimeZoneMode": "Unlock",
    }
    if override_type == "Time":
        payload["Minutes"] = override_mins

    try:
        await _request_with_reauth(
            hass, "POST", url, json=payload, timeout=10
        )
        _LOGGER.info("Override %s sent to doors %s", override_type, door_ids)
        return True
    except Exception as e:
        _LOGGER.error("Error in set_override: %s", e)
        return False


async def override_until_resume_card_or_pin(hass, door_ids: list[int]) -> bool:
    """
    Override doors until resume via CardOrPin.
    """
    base_url = hass.data[DOMAIN]["base_url"]
    url      = f"{base_url}/api/PanelCommands/OverrideDoor"
    payload  = {
        "DoorIds":      door_ids,
        "OverrideType": "Resume",
        "TimeZoneMode": "CardOrPin",
    }

    try:
        await _request_with_reauth(
            hass, "POST", url, json=payload, timeout=10
        )
        _LOGGER.info("Override CardOrPin sent to doors %s", door_ids)
        return True
    except Exception as e:
        _LOGGER.error("Error in override_until_resume_card_or_pin: %s", e)
        return False


async def resume_schedule(hass, door_ids: list[int]) -> bool:
    """
    Resume door schedule via PanelCommands/ResumeDoor.
    """
    base_url = hass.data[DOMAIN]["base_url"]
    url      = f"{base_url}/api/PanelCommands/ResumeDoor"
    payload  = {"DoorIds": door_ids}

    try:
        await _request_with_reauth(
            hass, "POST", url, json=payload, timeout=10
        )
        _LOGGER.info("Resumed schedule for doors: %s", door_ids)
        return True
    except Exception as e:
        _LOGGER.error("Error in resume_schedule: %s", e)
        return False

import httpx
import logging

_LOGGER = logging.getLogger(__name__)


async def pulse_unlock(hass, door_ids: list[int]) -> bool:
    base_url = hass.data["protector_net"]["base_url"]
    session = hass.data["protector_net"]["session_cookie"]

    headers = {
        "Content-Type": "application/json",
        "Cookie": f"ss-id={session}"
    }

    url = f"{base_url}/api/PanelCommands/PulseDoor"
    payload = { "DoorIds": door_ids }

    try:
        async with httpx.AsyncClient(verify=False) as client:
            response = await client.post(url, headers=headers, json=payload)
            if response.status_code == 200:
                _LOGGER.info(f"Pulse unlock sent for doors: {door_ids}")
                return True
            else:
                _LOGGER.error(f"Pulse unlock failed: {response.status_code} - {response.text}")
                return False
    except Exception as e:
        _LOGGER.exception(f"Error in pulse_unlock: {e}")
        return False


async def set_override(hass, door_ids: list[int], override_type: str, minutes: int = None) -> bool:
    base_url = hass.data["protector_net"]["base_url"]
    session = hass.data["protector_net"]["session_cookie"]

    headers = {
        "Content-Type": "application/json",
        "Cookie": f"ss-id={session}"
    }

    valid_override_types = ["Time", "Schedule", "Resume"]
    if override_type not in valid_override_types:
        _LOGGER.error(f"Invalid override_type: {override_type}")
        return False

    payload = {
        "OverrideType": override_type,
        "DoorIds": door_ids
    }

    if override_type in ["Time", "Schedule", "Resume"]:
        payload["TimeZoneMode"] = "Unlock"

    if override_type == "Time":
        payload["Minutes"] = minutes or hass.data["protector_net"].get("override_minutes", 5)

    try:
        async with httpx.AsyncClient(verify=False) as client:
            response = await client.post(
                url=f"{base_url}/api/PanelCommands/OverrideDoor",
                headers=headers,
                json=payload
            )
            if response.status_code == 200:
                _LOGGER.info(f"Override '{override_type}' sent to doors {door_ids}")
                return True
            else:
                _LOGGER.error(f"Override failed: {response.status_code} - {response.text}")
                return False
    except Exception as e:
        _LOGGER.exception(f"Error in set_override: {e}")
        return False



async def resume_schedule(hass, door_ids: list[int]) -> bool:
    base_url = hass.data["protector_net"]["base_url"]
    session = hass.data["protector_net"]["session_cookie"]

    headers = {
        "Content-Type": "application/json",
        "Cookie": f"ss-id={session}"
    }

    url = f"{base_url}/api/PanelCommands/ResumeDoor"
    payload = { "DoorIds": door_ids }

    try:
        async with httpx.AsyncClient(verify=False) as client:
            response = await client.post(url, headers=headers, json=payload)
            if response.status_code == 200:
                _LOGGER.info(f"Resumed schedule for doors: {door_ids}")
                return True
            else:
                _LOGGER.error(f"Resume failed: {response.status_code} - {response.text}")
                return False
    except Exception as e:
        _LOGGER.exception(f"Error in resume_schedule: {e}")
        return False


async def get_all_doors(hass):
    base_url = hass.data["protector_net"]["base_url"]
    session = hass.data["protector_net"]["session_cookie"]

    headers = {
        "Content-Type": "application/json",
        "Cookie": f"ss-id={session}"
    }

    url = f"{base_url}/api/doors?"
    try:
        async with httpx.AsyncClient(verify=False) as client:
            response = await client.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            return data.get("Results", [])
    except Exception as e:
        _LOGGER.exception(f"Error fetching doors: {e}")
        return []

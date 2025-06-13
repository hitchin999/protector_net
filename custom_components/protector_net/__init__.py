from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform

from .const import DOMAIN, DEFAULT_OVERRIDE_MINUTES

PLATFORMS = [Platform.BUTTON]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["base_url"] = entry.data["base_url"]
    hass.data[DOMAIN]["session_cookie"] = entry.data["session_cookie"]
    hass.data[DOMAIN]["override_minutes"] = entry.data.get(
        "override_minutes", DEFAULT_OVERRIDE_MINUTES
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.pop(DOMAIN, None)
    return unload_ok

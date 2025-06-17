# custom_components/protector_net/__init__.py

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform

from .const import DOMAIN, DEFAULT_OVERRIDE_MINUTES

PLATFORMS = [Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Store config per-entry and forward to the button platform."""
    hass.data.setdefault(DOMAIN, {})

    # carve out one dict slot just for this entry
    entry_data = hass.data[DOMAIN].setdefault(entry.entry_id, {})

    # core settings
    entry_data["base_url"]       = entry.data["base_url"]
    entry_data["username"]       = entry.data["username"]
    entry_data["password"]       = entry.data["password"]
    entry_data["session_cookie"] = entry.data["session_cookie"]
    entry_data["partition_id"]   = entry.data["partition_id"]

    # options
    entry_data["override_minutes"] = entry.options.get(
        "override_minutes", DEFAULT_OVERRIDE_MINUTES
    )

    # forward setup to our platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload platforms and remove this entry’s data."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        # remove just this entry’s slot
        hass.data[DOMAIN].pop(entry.entry_id, None)
        # if no entries remain, clean up the top-level key
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)
    return unload_ok

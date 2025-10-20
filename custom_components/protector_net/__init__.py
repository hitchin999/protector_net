from __future__ import annotations

from urllib.parse import urlsplit

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, DEFAULT_OVERRIDE_MINUTES

PLATFORMS: list[Platform] = [Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Store config per-entry, register a hub device (per entry), and forward to platforms."""
    hass.data.setdefault(DOMAIN, {})
    entry_data = hass.data[DOMAIN].setdefault(entry.entry_id, {})

    # --- core settings ---
    base_url = entry.data["base_url"].rstrip("/")
    entry_data["base_url"] = base_url
    entry_data["username"] = entry.data["username"]
    entry_data["password"] = entry.data["password"]
    entry_data["session_cookie"] = entry.data["session_cookie"]
    entry_data["partition_id"] = entry.data["partition_id"]

    # --- options ---
    entry_data["override_minutes"] = entry.options.get(
        "override_minutes", DEFAULT_OVERRIDE_MINUTES
    )

    # --- derive a stable host and entry-scoped hub identifier ---
    split = urlsplit(base_url)
    host = split.netloc or split.path  # works for http://ip and https://host:port
    hub_identifier = f"hub:{host}|{entry.entry_id}"

    # expose for platforms (buttons will use via_device -> this hub)
    entry_data["host"] = host
    entry_data["hub_identifier"] = hub_identifier

    # --- create/get the top-level "hub" device for this entry ---
    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, hub_identifier)},  # entry-scoped => avoids cross-entry merge
        manufacturer="Hartmann Controls",
        model="Protector.Net",
        name=f"Protector.Net ({host})",
        configuration_url=base_url,
    )

    # forward setup to our platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload platforms and remove this entry’s data."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)
    return unload_ok

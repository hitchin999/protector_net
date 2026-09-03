# custom_components/protector_net/compat.py
"""Device-registry compatibility shims.

Home Assistant 2026.8 reworked how a device declares its parent ("via") device:

* ``DeviceInfo["via_device"]`` — an identifier *tuple* — is deprecated and stops
  working in 2027.8. Its replacement is ``via_device_id``, the registry id of an
  already-registered device.
* ``DeviceRegistry.async_get_device(identifiers=...)`` is deprecated for the same
  underlying reason (identifiers are no longer unique across config entries);
  ``async_get_device_by_identifier(identifier, config_entry_id)`` replaces it.

The ``via_device`` deprecation is *not* a soft warning for us: HA raises inside
``async_get_or_create``, ``entity_platform`` catches it and calls
``add_to_platform_abort()``, and the entity is silently dropped — which showed up
as "Error adding entity None for domain sensor" and a pile of missing entities.

We can't simply switch keys, because a ``via_device_id`` key on an older core
raises ``TypeError`` and drops the entity just as hard. So probe the running
core once and emit whichever form it understands.
"""

from __future__ import annotations

import inspect
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN

# True on HA >= 2026.8, where async_get_or_create accepts `via_device_id`.
SUPPORTS_VIA_DEVICE_ID: bool = "via_device_id" in inspect.signature(
    dr.DeviceRegistry.async_get_or_create
).parameters


@callback
def async_get_device_by_identifier(
    hass: HomeAssistant, identifier: tuple[str, str], entry_id: str
) -> dr.DeviceEntry | None:
    """Look up one of our own devices by identifier, scoped to the config entry.

    Falls back to the deprecated registry-wide lookup on cores that predate
    ``async_get_device_by_identifier``.
    """
    reg = dr.async_get(hass)
    getter = getattr(reg, "async_get_device_by_identifier", None)
    if getter is not None:
        return getter(identifier, entry_id)
    return reg.async_get_device(identifiers={identifier})


@callback
def async_hub_identifier(hass: HomeAssistant, entry_id: str) -> str | None:
    """Return the hub device's identifier string for this entry."""
    return (hass.data.get(DOMAIN, {}).get(entry_id) or {}).get("hub_identifier")


@callback
def async_hub_device_id(hass: HomeAssistant, entry_id: str) -> str | None:
    """Return the registry id of this entry's hub device, if it is registered."""
    hub_identifier = async_hub_identifier(hass, entry_id)
    if not hub_identifier:
        return None
    device = async_get_device_by_identifier(hass, (DOMAIN, hub_identifier), entry_id)
    return device.id if device else None


@callback
def async_via_hub(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    """Return the device_info fragment linking a child device to the hub device.

    Empty when the hub device isn't registered yet — a dangling ``via_device_id``
    is rejected by the registry, and no link is far better than a dropped entity.
    ``async_ensure_hub_device`` runs before platform setup so this is the
    exceptional case, not the normal one.
    """
    hub_identifier = async_hub_identifier(hass, entry_id)
    if not hub_identifier:
        return {}

    if not SUPPORTS_VIA_DEVICE_ID:
        return {"via_device": (DOMAIN, hub_identifier)}

    hub_device_id = async_hub_device_id(hass, entry_id)
    return {"via_device_id": hub_device_id} if hub_device_id else {}


@callback
def async_ensure_hub_device(
    hass: HomeAssistant, entry, partition_name: str, base_url: str
) -> str | None:
    """Register the hub device up front and return its registry id.

    Door devices reference the hub by registry id, so the hub has to exist
    before any platform adds a door entity. Platform setup order isn't
    guaranteed to put a hub entity first, so we create it explicitly. The hub
    entities' own device_info later resolves to this same device (identical
    identifiers), so this only front-loads the registration.
    """
    hub_identifier = async_hub_identifier(hass, entry.entry_id)
    if not hub_identifier:
        return None

    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, hub_identifier)},
        manufacturer="Yoel Goldstein/Vaayer LLC",
        model="Protector.Net Hub",
        name=f"Hub Status – {partition_name}",
        configuration_url=base_url,
    )
    return device.id

from homeassistant.helpers.entity import Entity
from .const import DOMAIN

class ProtectorNetDevice(Entity):
    """
    Lightweight base: keep entry context & helpers.
    Do NOT set _attr_device_info here — each concrete entity will supply
    proper device_info (hub or per-door) so devices group correctly.
    """
    def __init__(self, config_entry):
        self._entry = config_entry
        self._entry_id = config_entry.entry_id

    # Convenience helpers if you want them in other platforms later
    def _entry_data(self, hass):
        return hass.data[DOMAIN][self._entry_id]

    def get_host_key(self, hass) -> str | None:
        return self._entry_data(hass).get("host")

    def get_hub_identifier(self, hass) -> str | None:
        return self._entry_data(hass).get("hub_identifier")

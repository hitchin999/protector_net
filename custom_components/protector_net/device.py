# custom_components/protector_net/device.py
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from .const import DOMAIN

class ProtectorNetDevice(Entity):
    def __init__(self, config_entry):
        # use the entry_id so each install is its own device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, config_entry.entry_id)},
            name=f"Protector.Net @ {config_entry.data['base_url']}",
            manufacturer="Yoel Goldstein/Vaayer LLC",
            model="Protector.Net Integration",
            entry_type="service",
        )

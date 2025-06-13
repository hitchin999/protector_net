# custom_components/protector_net/device.py

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from .const import DOMAIN

class ProtectorNetDevice(Entity):
    """Mixin for all Protector.Net entities to share one DeviceInfo."""

    _attr_device_info = DeviceInfo(
        identifiers={(DOMAIN, "protector_net_main")},
        name="Protector.Net Access Control",
        manufacturer="Yoel Goldstein/Vaayer LLC",
        model="Protector.Net Integration",
        entry_type="service",
    )

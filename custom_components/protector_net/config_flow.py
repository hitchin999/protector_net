# custom_components/protector_net/config_flow.py
import voluptuous as vol
from urllib.parse import urlparse
from homeassistant import config_entries
from homeassistant.helpers import config_validation as cv
from .const import DOMAIN, DEFAULT_OVERRIDE_MINUTES
from . import api

# Define all available entity types with user-friendly labels
ENTITY_CHOICES = {
    "_pulse_unlock": "Pulse Unlock",
    "_resume_schedule": "Resume Schedule",
    "_unlock_until_resume": "Unlock Until Resume",
    "_override_card_or_pin": "CardOrPin Until Resume",
    "_unlock_until_next_schedule": "Unlock Until Next Schedule",
    "_timed_override_unlock": "Timed Override Unlock",
}

class ProtectorNetConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle Protector.Net config flow: login, partition & entity selection."""
    VERSION = 1

    def __init__(self):
        self._base_url = None
        self._username = None
        self._password = None
        self._override_mins = None
        self._session_cookie = None
        self._partitions = {}

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input:
            self._base_url = user_input["base_url"]
            self._username = user_input["username"]
            self._password = user_input["password"]
            self._override_mins = user_input["override_minutes"]

            try:
                self._session_cookie = await api.login(
                    self.hass,
                    self._base_url,
                    self._username,
                    self._password
                )
            except Exception:
                errors["base"] = "cannot_connect"

            if errors:
                return self.async_show_form(
                    step_id="user",
                    data_schema=vol.Schema({
                        vol.Required("base_url"): str,
                        vol.Required("username"): str,
                        vol.Required("password"): str,
                        vol.Optional("override_minutes", default=DEFAULT_OVERRIDE_MINUTES): int,
                    }),
                    errors=errors,
                )

            parts = await api.get_partitions(
                self.hass,
                self._base_url,
                self._session_cookie
            )
            self._partitions = {p["Id"]: p["Name"] for p in parts}

            return await self.async_step_partition()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("base_url"): str,
                vol.Required("username"): str,
                vol.Required("password"): str,
                vol.Optional("override_minutes", default=DEFAULT_OVERRIDE_MINUTES): int,
            }),
            errors=errors,
        )

    async def async_step_partition(self, user_input=None):
        if user_input:
            partition_id = user_input["partition"]
            partition_name = self._partitions[partition_id]
            host = urlparse(self._base_url).netloc
            title = f"{host} – {partition_name}"

            self.context["entry_data"] = {
                "base_url": self._base_url,
                "username": self._username,
                "password": self._password,
                "session_cookie": self._session_cookie,
                "partition_id": partition_id,
            }
            self.context["entry_options"] = {"override_minutes": self._override_mins}

            return await self.async_step_entity_selection()

        return self.async_show_form(
            step_id="partition",
            data_schema=vol.Schema({
                vol.Required("partition", default=list(self._partitions)[0]): vol.In(self._partitions),
            }),
        )

    async def async_step_entity_selection(self, user_input=None):
        """Let the user pick which Protector.Net entities to enable."""
        if user_input:
            data = self.context["entry_data"]
            options = self.context["entry_options"]
            options["entities"] = user_input["entities"]

            return self.async_create_entry(
                title=data["base_url"],
                data=data,
                options=options
            )

        return self.async_show_form(
            step_id="entity_selection",
            data_schema=vol.Schema({
                vol.Required(
                    "entities",
                    default=list(ENTITY_CHOICES.keys())
                ): cv.multi_select(ENTITY_CHOICES)
            }),
            description_placeholders={"info": "Select which Protector.Net entities to create."},
        )

    @staticmethod
    @config_entries.callback
    def async_get_options_flow(config_entry):
        return ProtectorNetOptionsFlow(config_entry)


class ProtectorNetOptionsFlow(config_entries.OptionsFlow):
    """Handle updating override_minutes and entity selection after setup."""
    def __init__(self, entry):
        self.entry = entry

    async def async_step_init(self, user_input=None):
        if user_input:
            return self.async_create_entry(title="", data=user_input)

        current_entities = self.entry.options.get("entities", [])
        current_override = self.entry.options.get("override_minutes", DEFAULT_OVERRIDE_MINUTES)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(
                    "override_minutes",
                    default=current_override
                ): int,
                vol.Required(
                    "entities",
                    default=current_entities
                ): cv.multi_select(ENTITY_CHOICES)
            }),
            description_placeholders={"info": "Adjust which Protector.Net entities to import and override duration."},
        )

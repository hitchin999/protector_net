import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from .const import DOMAIN, DEFAULT_OVERRIDE_MINUTES


class ProtectorNetConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}

        if user_input is not None:
            # Split override_minutes into options
            data = {
                "base_url": user_input["base_url"],
                "session_cookie": user_input["session_cookie"]
            }
            options = {
                "override_minutes": user_input.get("override_minutes", DEFAULT_OVERRIDE_MINUTES)
            }
            return self.async_create_entry(title="Protector.Net", data=data, options=options)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("base_url"): str,
                vol.Required("session_cookie"): str,
                vol.Optional("override_minutes", default=DEFAULT_OVERRIDE_MINUTES): int,
            }),
            errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ProtectorNetOptionsFlow(config_entry)


class ProtectorNetOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        errors = {}

        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        override_minutes = self.config_entry.options.get(
            "override_minutes",
            DEFAULT_OVERRIDE_MINUTES
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional("override_minutes", default=override_minutes): int,
            }),
            errors=errors
        )

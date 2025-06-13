# custom_components/protector_net/config_flow.py

import voluptuous as vol
from homeassistant import config_entries
from .const import DOMAIN, DEFAULT_OVERRIDE_MINUTES
from . import api

class ProtectorNetConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a Protector.Net config flow, cookie-based login + partition selection."""
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            self._base_url      = user_input["base_url"]
            self._username      = user_input["username"]
            self._password      = user_input["password"]
            self._override_mins = user_input["override_minutes"]

            # 1) Log in, grab ss-id
            try:
                self._session_cookie = await api.login(
                    self.hass,
                    self._base_url,
                    self._username,
                    self._password
                )
            except Exception:
                errors["base"] = "cannot_connect"
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

            # 2) Fetch partitions
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
        """Let the user pick which partition (of the ones they have rights to)."""
        if user_input is not None:
            data = {
                "base_url":       self._base_url,
                "username":       self._username,
                "password":       self._password,
                "session_cookie": self._session_cookie,
                "partition_id":   user_input["partition"],
            }
            options = {"override_minutes": self._override_mins}
            return self.async_create_entry(title="Protector.Net", data=data, options=options)

        return self.async_show_form(
            step_id="partition",
            data_schema=vol.Schema({
                vol.Required("partition", default=list(self._partitions)[0]): vol.In(self._partitions),
            }),
        )

    @staticmethod
    @config_entries.callback
    def async_get_options_flow(config_entry):
        return ProtectorNetOptionsFlow(config_entry)


class ProtectorNetOptionsFlow(config_entries.OptionsFlow):
    """Handle updating override_minutes after initial setup."""
    def __init__(self, entry):
        self.entry = entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(
                    "override_minutes",
                    default=self.entry.options.get("override_minutes", DEFAULT_OVERRIDE_MINUTES)
                ): int,
            }),
        )

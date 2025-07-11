# custom_components/protector_net/config_flow.py
import logging
import voluptuous as vol

from urllib.parse import urlparse

from homeassistant import config_entries
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, DEFAULT_OVERRIDE_MINUTES, KEY_PLAN_IDS
from . import api

_LOGGER = logging.getLogger(__name__)

ENTITY_CHOICES = {
    "_pulse_unlock":               "Pulse Unlock",
    "_resume_schedule":            "Resume Schedule",
    "_unlock_until_resume":        "Unlock Until Resume",
    "_override_card_or_pin":       "CardOrPin Until Resume",
    "_unlock_until_next_schedule": "Unlock Until Next Schedule",
    "_timed_override_unlock":      "Timed Override Unlock",
}


class ProtectorNetConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle Protector.Net config flow: login, partition, plans & entity selection."""

    VERSION = 1

    def __init__(self):
        self._base_url = None
        self._username = None
        self._password = None
        self._override_mins = None
        self._session_cookie = None
        self._partitions = {}
        self._plans = {}

    async def async_step_user(self, user_input=None):
        errors = {}

        user_schema = vol.Schema({
            vol.Required(
                "base_url",
                description={"suggested_value": "https://doors.example.com:11001"}
            ): str,
            vol.Required("username"): str,
            vol.Required("password"): str,
            vol.Optional("override_minutes", default=DEFAULT_OVERRIDE_MINUTES): int,
        })

        if user_input:
            self._base_url      = user_input["base_url"]
            self._username      = user_input["username"]
            self._password      = user_input["password"]
            self._override_mins = user_input["override_minutes"]

            try:
                self._session_cookie = await api.login(
                    self.hass, self._base_url, self._username, self._password
                )
            except Exception:
                errors["base"] = "cannot_connect"

            if errors:
                return self.async_show_form(
                    step_id="user",
                    data_schema=user_schema,
                    errors=errors,
                )

            parts = await api.get_partitions(
                self.hass, self._base_url, self._session_cookie
            )
            # UI always sends selected keys as strings
            self._partitions = {str(p["Id"]): p["Name"] for p in parts}
            return await self.async_step_partition()

        return self.async_show_form(
            step_id="user",
            data_schema=user_schema,
            errors=errors,
        )

    async def async_step_partition(self, user_input=None):
        if user_input:
            partition_key  = user_input["partition"]
            partition_name = self._partitions[partition_key]
            partition_id   = int(partition_key)

            host = urlparse(self._base_url).netloc
            self.context["entry_title"] = f"{host} – {partition_name}"

            # Persist data & options so far
            self.context["entry_data"] = {
                "base_url":       self._base_url,
                "username":       self._username,
                "password":       self._password,
                "session_cookie": self._session_cookie,
                "partition_id":   partition_id,
            }
            self.context["entry_options"] = {
                "override_minutes": self._override_mins
            }

            return await self.async_step_plans()

        return self.async_show_form(
            step_id="partition",
            data_schema=vol.Schema({
                vol.Required("partition", default=list(self._partitions)[0]):
                    vol.In(self._partitions),
            }),
        )

    async def async_step_plans(self, user_input=None):
        errors = {}
        if not self._plans:
            # Attempt to fetch trigger plans, re-login on 401 if needed
            try:
                raw = await api.get_action_plans(
                    self.hass,
                    self.context["entry_data"]["base_url"],
                    self.context["entry_data"]["session_cookie"],
                    self.context["entry_data"]["partition_id"],
                )
            except Exception as err:
                _LOGGER.debug("Fetching plans failed, retrying login: %s", err)
                # try one re-login
                try:
                    self._session_cookie = await api.login(
                        self.hass,
                        self._base_url,
                        self._username,
                        self._password
                    )
                    # update saved cookie for subsequent calls
                    self.context["entry_data"]["session_cookie"] = self._session_cookie
                    raw = await api.get_action_plans(
                        self.hass,
                        self._base_url,
                        self._session_cookie,
                        self.context["entry_data"]["partition_id"],
                    )
                except Exception as err2:
                    _LOGGER.error("Could not fetch action plans: %s", err2)
                    errors["base"] = "cannot_connect"
                    # return to same form with error
                    return self.async_show_form(
                        step_id="plans",
                        data_schema=vol.Schema({
                            vol.Required("plans", default=[]): cv.multi_select({})
                        }),
                        errors=errors
                    )

            # Filter out any System‐type “HA Door Log” and only keep Trigger plans
            triggers = [
                p for p in raw
                if p.get("PlanType") == "Trigger"
                   and p.get("Name") != "HA Door Log"
            ]
            self._plans = {str(p["Id"]): p["Name"] for p in triggers}

        if user_input is not None:
            plan_ids = [int(pid) for pid in user_input["plans"]]
            # store for runtime & for reconfigure defaults
            self.context["entry_data"][KEY_PLAN_IDS]   = plan_ids
            self.context["entry_options"][KEY_PLAN_IDS] = plan_ids
            return await self.async_step_entity_selection()

        return self.async_show_form(
            step_id="plans",
            data_schema=vol.Schema({
                vol.Required("plans", default=list(self._plans.keys())):
                    cv.multi_select(self._plans),
            }),
            description_placeholders={
                "info": "Select which action plans to turn into buttons."
            },
            errors=errors,
        )

    async def async_step_entity_selection(self, user_input=None):
        if user_input is not None:
            data    = self.context["entry_data"]
            options = self.context["entry_options"]
            options["entities"] = user_input["entities"]
            data["entities"]    = user_input["entities"]

            return self.async_create_entry(
                title=self.context.get("entry_title", self._base_url),
                data=data,
                options=options,
            )

        return self.async_show_form(
            step_id="entity_selection",
            data_schema=vol.Schema({
                vol.Required("entities", default=list(ENTITY_CHOICES.keys())):
                    cv.multi_select(ENTITY_CHOICES),
            }),
            description_placeholders={
                "info": "Select which Protector.Net door entities to create."
            },
        )

    @staticmethod
    @config_entries.callback
    def async_get_options_flow(config_entry):
        return ProtectorNetOptionsFlow(config_entry)


class ProtectorNetOptionsFlow(config_entries.OptionsFlow):
    """Allow editing override_minutes, door-entities, and action-plan selection."""

    def __init__(self, entry):
        self.entry = entry
        self._plan_choices = {}
    async def async_step_init(self, user_input=None):
        # Use the runtime (reauth-aware) fetch, not the config-flow GET
        raw = await api.get_action_plans(self.hass, self.entry.entry_id)

        triggers = [
            p for p in raw
            if p.get("PlanType") == "Trigger"
               and p.get("Name") != "HA Door Log"
        ]
        self._plan_choices = {str(p["Id"]): p["Name"] for p in triggers}

        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        default_override = self.entry.options.get(
            "override_minutes", DEFAULT_OVERRIDE_MINUTES
        )
        default_entities = self.entry.options.get(
            "entities", self.entry.data.get("entities", [])
        )
        default_plans = [
            str(x)
            for x in self.entry.options.get(
                KEY_PLAN_IDS,
                self.entry.data.get(KEY_PLAN_IDS, []),
            )
        ]

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional("override_minutes", default=default_override): int,
                vol.Required("entities", default=default_entities):
                    cv.multi_select(ENTITY_CHOICES),
                vol.Required(KEY_PLAN_IDS, default=default_plans):
                    cv.multi_select(self._plan_choices),
            }),
            description_placeholders={
                "info": "Adjust which door entities, action plans, and override duration to use."
            },
        )
        

import logging
import voluptuous as vol

from urllib.parse import urlparse, urlsplit

from homeassistant import config_entries
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, DEFAULT_OVERRIDE_MINUTES, KEY_PLAN_IDS
from . import api

_LOGGER = logging.getLogger(__name__)

# Optional legacy buttons only (Pulse Unlock is implicit and always added)
ENTITY_CHOICES_OPTIONAL = {
    "_resume_schedule":            "Resume Schedule",
    "_unlock_until_resume":        "Unlock Until Resume",
    "_override_card_or_pin":       "CardOrPin Until Resume",
    "_unlock_until_next_schedule": "Unlock Until Next Schedule",
    "_timed_override_unlock":      "Timed Override Unlock",
}


class ProtectorNetConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle Protector.Net config flow: login, partition, plans & (minimal) entity selection."""

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
            # normalize base_url once
            self._base_url      = user_input["base_url"].rstrip("/")
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

            # set per-entry unique_id to avoid dupes across same host+partition
            split = urlsplit(self._base_url)
            host_for_uid = split.netloc or split.path
            unique_id = f"{host_for_uid}|partition:{partition_id}"
            await self.async_set_unique_id(unique_id, raise_on_progress=False)
            self._abort_if_unique_id_configured()

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

            # Filter out any System “HA Door Log” and only keep Trigger plans
            triggers = [
                p for p in raw
                if p.get("PlanType") == "Trigger"
                   and p.get("Name") != "HA Door Log"
            ]
            self._plans = {str(p["Id"]): p["Name"] for p in triggers}

        if user_input is not None:
            plan_ids = [int(pid) for pid in user_input["plans"]]
            # store for runtime & for reconfigure defaults
            self.context["entry_data"][KEY_PLAN_IDS]    = plan_ids
            self.context["entry_options"][KEY_PLAN_IDS] = plan_ids
            return await self.async_step_entity_selection()

        return self.async_show_form(
            step_id="plans",
            data_schema=vol.Schema({
                vol.Required("plans", default=list(self._plans.keys())):
                    cv.multi_select(self._plans),
            }),
            description_placeholders={
                "info": "Select which trigger plans to clone (as System) and expose as Action Plan buttons."
            },
            errors=errors,
        )

    async def async_step_entity_selection(self, user_input=None):
        """Pick *optional* legacy door buttons. Pulse Unlock is always added."""
        if user_input is not None:
            data    = self.context["entry_data"]
            options = self.context["entry_options"]

            # keep only valid optional keys
            picked_optional = [e for e in user_input["entities"] if e in ENTITY_CHOICES_OPTIONAL]
            # ALWAYS include Pulse Unlock
            final_entities = ["_pulse_unlock", *picked_optional]

            options["entities"] = final_entities
            data["entities"]    = final_entities

            return self.async_create_entry(
                title=self.context.get("entry_title", self._base_url),
                data=data,
                options=options,
            )

        # Default: nothing selected
        return self.async_show_form(
            step_id="entity_selection",
            data_schema=vol.Schema({
                vol.Required("entities", default=[]): cv.multi_select(ENTITY_CHOICES_OPTIONAL),
            }),
            description_placeholders={
                "info": "Select any additional legacy door buttons you want. "
                        "Pulse Unlock is always added automatically."
            },
        )

    @staticmethod
    @config_entries.callback
    def async_get_options_flow(config_entry):
        return ProtectorNetOptionsFlow(config_entry)


class ProtectorNetOptionsFlow(config_entries.OptionsFlow):
    """Allow editing override_minutes, optional legacy door buttons, and action-plan selection."""

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
            # Force Pulse Unlock to remain present; keep only valid optional picks
            picked_optional = [e for e in user_input.get("entities", []) if e in ENTITY_CHOICES_OPTIONAL]
            user_input["entities"] = ["_pulse_unlock", *picked_optional]
            return self.async_create_entry(title="", data=user_input)

        default_override = self.entry.options.get(
            "override_minutes", DEFAULT_OVERRIDE_MINUTES
        )

        saved_entities = self.entry.options.get(
            "entities", self.entry.data.get("entities", [])
        ) or []

        # Show only optional ones in the picker (Pulse Unlock is implicit)
        default_entities_optional = [e for e in saved_entities if e in ENTITY_CHOICES_OPTIONAL]

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
                vol.Required("entities", default=default_entities_optional):
                    cv.multi_select(ENTITY_CHOICES_OPTIONAL),
                vol.Required(KEY_PLAN_IDS, default=default_plans):
                    cv.multi_select(self._plan_choices),
            }),
            description_placeholders={
                "info": "Adjust Action Plan buttons and override duration. "
                        "Pulse Unlock is always included automatically."
            },
        )

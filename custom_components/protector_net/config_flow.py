import logging
import voluptuous as vol

from urllib.parse import urlparse, urlsplit

from homeassistant import config_entries
from homeassistant.data_entry_flow import section
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, DEFAULT_OVERRIDE_MINUTES, DEFAULT_PIN_DIGITS, KEY_PLAN_IDS, KEY_AUTO_ADD_NEW_DOORS
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
        self._pin_digits = None
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
            vol.Optional("pin_digits", default=DEFAULT_PIN_DIGITS): int,
        })

        if user_input:
            # normalize base_url once
            self._base_url      = user_input["base_url"].rstrip("/")
            self._username      = user_input["username"]
            self._password      = user_input["password"]
            self._override_mins = user_input["override_minutes"]
            self._pin_digits    = user_input.get("pin_digits", DEFAULT_PIN_DIGITS)

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
                "override_minutes": self._override_mins,
                "pin_digits": self._pin_digits,
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
        )

    @staticmethod
    @config_entries.callback
    def async_get_options_flow(config_entry):
        return ProtectorNetOptionsFlow(config_entry)


class ProtectorNetOptionsFlow(config_entries.OptionsFlow):
    """Multi-step options flow:
       - menu: pick basic settings or door time zones
       - basic_settings: override minutes / pin digits / entities / action plans
       - door_time_zones: provision + activate HA Door Time Zones
                          (with explicit enable gates so opening the page
                          can't accidentally trigger a reconcile)
    """

    def __init__(self, entry):
        self.entry = entry
        self._plan_choices = {}
        self._door_choices: dict[str, str] = {}  # {door_id_str: "Name (current TZ)"}
        self._door_names: dict[int, str] = {}    # {door_id: "Name"}

    # ------------------------------------------------------------------
    # Entry point: menu
    # ------------------------------------------------------------------
    async def async_step_init(self, user_input=None):
        return self.async_show_menu(
            step_id="init",
            menu_options=["basic_settings", "door_time_zones"],
        )

    # ------------------------------------------------------------------
    # Step 1: basic settings (the original options form, minus schedules)
    # ------------------------------------------------------------------
    async def async_step_basic_settings(self, user_input=None):
        # Action plans are needed to render the picker.
        raw = await api.get_action_plans(self.hass, self.entry.entry_id)
        triggers = [
            p for p in raw
            if p.get("PlanType") == "Trigger"
               and p.get("Name") != "HA Door Log"
        ]
        self._plan_choices = {str(p["Id"]): p["Name"] for p in triggers}

        if user_input is not None:
            # The entities field is wrapped in a section() — the frontend
            # nests its value under the section key, so dig in to extract.
            entities_section = user_input.pop("legacy_entities_section", None) or {}
            picked_raw = entities_section.get("entities", [])

            # Force Pulse Unlock to remain present; keep only valid optional picks
            picked_optional = [e for e in picked_raw if e in ENTITY_CHOICES_OPTIONAL]
            user_input["entities"] = ["_pulse_unlock", *picked_optional]

            # Preserve schedule-related options that aren't on this page —
            # async_create_entry replaces the entire options dict, so we
            # have to merge.
            preserved_keys = ("managed_doors", KEY_AUTO_ADD_NEW_DOORS)
            for k in preserved_keys:
                if k in self.entry.options:
                    user_input[k] = self.entry.options[k]

            return self.async_create_entry(title="", data=user_input)

        default_override = self.entry.options.get(
            "override_minutes", DEFAULT_OVERRIDE_MINUTES
        )
        default_pin_digits = self.entry.options.get(
            "pin_digits", DEFAULT_PIN_DIGITS
        )
        saved_entities = self.entry.options.get(
            "entities", self.entry.data.get("entities", [])
        ) or []
        default_entities_optional = [e for e in saved_entities if e in ENTITY_CHOICES_OPTIONAL]
        default_plans = [
            str(x)
            for x in self.entry.options.get(
                KEY_PLAN_IDS,
                self.entry.data.get(KEY_PLAN_IDS, []),
            )
        ]

        return self.async_show_form(
            step_id="basic_settings",
            data_schema=vol.Schema({
                vol.Optional("override_minutes", default=default_override): int,
                vol.Optional("pin_digits", default=default_pin_digits): int,
                vol.Required(KEY_PLAN_IDS, default=default_plans):
                    cv.multi_select(self._plan_choices),
                # Wrap the legacy entities multi-select in a section() so the
                # warning description renders reliably above it. cv.multi_select
                # has a frontend bug (#16594) that suppresses data_description
                # rendering when the field renders as a checkbox-list, so the
                # only way to get the warning visible is via a section()'s own
                # description (which renders correctly).
                vol.Required("legacy_entities_section"): section(
                    vol.Schema({
                        vol.Required("entities", default=default_entities_optional):
                            cv.multi_select(ENTITY_CHOICES_OPTIONAL),
                    }),
                    {"collapsed": True},
                ),
            }),
        )

    # ------------------------------------------------------------------
    # Step 2: door time zones (provision + activate, with enable gates)
    # ------------------------------------------------------------------
    async def async_step_door_time_zones(self, user_input=None):
        # Fetch doors so we can offer them in the managed/active multi-selects.
        doors = await api.get_all_doors(self.hass, self.entry.entry_id)
        self._door_choices = {}
        self._door_names = {}
        managed_now = self.entry.options.get("managed_doors") or {}
        for d in doors:
            did = int(d.get("Id") or 0)
            if not did:
                continue
            name = str(d.get("Name") or f"Door {did}")
            self._door_names[did] = name

            # Annotate with current state for clarity in the picker.
            tag = ""
            info = managed_now.get(str(did)) or {}
            if info.get("active"):
                tag = " — active (HA-managed)"
            elif info:
                tag = " — provisioned (not yet active)"
            self._door_choices[str(did)] = f"{name}{tag}"

        if user_input is not None:
            managed_section = user_input.get("managed_doors_section") or {}
            active_section  = user_input.get("active_doors_section")  or {}

            # Enable gates: if a section's checkbox isn't ticked, that side
            # is left untouched. This preserves the existing managed_doors
            # state for whichever side the user didn't explicitly enable.
            apply_managed = bool(managed_section.get("apply_managed_changes", False))
            apply_active  = bool(active_section.get("apply_active_changes", False))

            # Compute the effective desired sets for reconcile():
            #  - If "apply" is on, use the new selection from this submit
            #  - If "apply" is off, use the current state (no change)
            current_managed_ids = [int(k) for k in managed_now.keys()]
            current_active_ids  = [int(k) for k, v in managed_now.items() if v.get("active")]

            if apply_managed:
                raw_managed = managed_section.get("managed_doors_select", []) or []
                desired_managed = [int(x) for x in raw_managed]
            else:
                desired_managed = current_managed_ids

            if apply_active:
                raw_active = active_section.get("active_doors_select", []) or []
                desired_active = [int(x) for x in raw_active]
            else:
                desired_active = current_active_ids

            # Active must be a subset of managed.
            desired_active = [d for d in desired_active if d in desired_managed]

            # Auto-add toggle (a separate top-level field, not inside a section).
            auto_add = bool(user_input.get(KEY_AUTO_ADD_NEW_DOORS, False))

            # Only run reconcile if at least one side was explicitly applied.
            # Otherwise we just persist the auto-add preference and exit.
            new_options = dict(self.entry.options)

            if apply_managed or apply_active:
                from . import managed_schedules
                summary = await managed_schedules.reconcile(
                    self.hass,
                    self.entry,
                    desired_managed_door_ids=desired_managed,
                    desired_active_door_ids=desired_active,
                    door_names=self._door_names,
                )
                _LOGGER.info(
                    "[%s] Door schedule reconcile: provisioned=%s activated=%s "
                    "deactivated=%s unprovisioned=%s failed=%s",
                    self.entry.entry_id,
                    summary.get("provisioned"), summary.get("activated"),
                    summary.get("deactivated"), summary.get("unprovisioned"),
                    summary.get("failed"),
                )
                new_options["managed_doors"] = summary.get("managed_doors", {})
            else:
                # No schedule changes requested — keep existing managed_doors.
                _LOGGER.debug(
                    "[%s] Door Time Zones page submitted with no enable "
                    "checkbox ticked; preserving managed_doors state",
                    self.entry.entry_id,
                )

            new_options[KEY_AUTO_ADD_NEW_DOORS] = auto_add

            return self.async_create_entry(title="", data=new_options)

        # Form defaults:
        # - Both "apply" gates default OFF (page can be opened safely without
        #   triggering anything).
        # - Both selects default to ALL doors pre-ticked. This is the bulk
        #   behavior the user wants: tick "apply" + submit = manage/activate
        #   everything; or untick the doors you don't want first.
        all_door_ids = list(self._door_choices.keys())

        # If the user already has managed doors saved, default the dropdown to
        # show their existing choices instead — they're editing, not bulk-adding.
        if managed_now:
            default_managed = [str(k) for k in managed_now.keys()]
            default_active  = [str(k) for k, v in managed_now.items() if v.get("active")]
        else:
            default_managed = all_door_ids
            default_active  = all_door_ids

        default_auto_add = bool(self.entry.options.get(KEY_AUTO_ADD_NEW_DOORS, False))

        return self.async_show_form(
            step_id="door_time_zones",
            data_schema=vol.Schema({
                vol.Required("managed_doors_section"): section(
                    vol.Schema({
                        vol.Optional("apply_managed_changes", default=False): bool,
                        vol.Optional("managed_doors_select", default=default_managed):
                            cv.multi_select(self._door_choices),
                    }),
                    {"collapsed": False},
                ),
                vol.Required("active_doors_section"): section(
                    vol.Schema({
                        vol.Optional("apply_active_changes", default=False): bool,
                        vol.Optional("active_doors_select", default=default_active):
                            cv.multi_select(self._door_choices),
                    }),
                    {"collapsed": False},
                ),
                vol.Optional(KEY_AUTO_ADD_NEW_DOORS, default=default_auto_add): bool,
            }),
        )

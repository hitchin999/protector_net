from __future__ import annotations

DOMAIN = "protector_net"

# Shared per-entry UI state bucket (kept in hass.data[DOMAIN][entry_id][UI_STATE])
UI_STATE = "ui_state"

# Back-compat: config flow & buttons/options expect this key to exist
KEY_PLAN_IDS = "plan_ids"

# Defaults
DEFAULT_OVERRIDE_MINUTES = 5
DEFAULT_OVERRIDE_TYPE = "For Specified Time"
DEFAULT_OVERRIDE_MODE = "Card"  # used only when an override is active

# --- Override Type / Mode options shown in the selects -----------------------

# Matches Protector.Net API "Type"
#  - "For Specified Time" -> Time (minutes used)
#  - "Until Resumed"      -> Resume
#  - "Until Next Schedule"-> Schedule
OVERRIDE_TYPE_OPTIONS = [
    "For Specified Time",
    "Until Resumed",
    "Until Next Schedule",
]

OVERRIDE_TYPE_LABEL_TO_TOKEN = {
    "for specified time": "Time",
    "until resumed": "Resume",
    "until next schedule": "Schedule",
}

# Override Mode options (reader modes) + UI "None" to indicate "not overridden"
OVERRIDE_MODE_OPTIONS = [
    "None",                 # UI-only: follow schedule / not overridden
    "Card",
    "Pin",
    "Unlock",
    "Card and Pin",
    "Card or Pin",
    "First Credential In",
    "Dual Credential",
    "Lockdown",
]

# Friendly label -> Protector.Net API token
OVERRIDE_MODE_LABEL_TO_TOKEN = {
    "card": "Card",
    "pin": "Pin",
    "unlock": "Unlock",
    "card and pin": "CardAndPin",
    "card or pin": "CardOrPin",
    "first credential in": "FirstCredentialIn",
    "dual credential": "DualCredential",
    "lockdown": "Lockdown",
    # "none" intentionally omitted: we never send an override for "None"
}

# --- Mapping between controller timeZone index and friendly reader mode ------

# Numbers seen on WS frames as "timeZone"
TZ_INDEX_TO_FRIENDLY = {
    0: "Lockdown",
    1: "Card",
    2: "Pin",
    3: "Card or Pin",
    4: "Card and Pin",
    5: "Unlock",
    6: "First Credential In",
    7: "Dual Credential",
    8: "Lockdown",  # some panels send 8 for lockdown as well
}

FRIENDLY_TO_TZ_INDEX = {v: k for k, v in TZ_INDEX_TO_FRIENDLY.items()}

# expose legacy per-door sensors (Lock State / Overridden / Reader Mode / Last Door Log)
KEY_EXPOSE_LEGACY_SENSORS = "expose_legacy_sensors"


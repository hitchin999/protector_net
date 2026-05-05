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
DEFAULT_PIN_DIGITS = 4  # Most PIN/prox readers default to 4 digits

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

# --- Managed Door Schedules feature -----------------------------------------
# Per-entry options key holding state for HA-managed door schedules.
#   options[KEY_MANAGED_DOORS] = {
#     "<door_id>": {
#         "ha_tz_id":        int,    # ID of the HA-created DoorTimeZone
#         "ha_tz_name":      str,    # name of the HA TZ (for matching/recovery)
#         "original_tz_id":  int,    # door's DoorTimeZoneId before we touched it
#         "active":          bool,   # whether door currently points at the HA TZ
#         "current_mode":    str,    # last mode applied to the HA TZ (API enum)
#     }
#   }
KEY_MANAGED_DOORS = "managed_doors"

# Options key: when True, the hourly name-sync also auto-provisions and
# auto-activates any new doors that appear in Hartmann after the integration
# was set up. Defaults to False — explicit opt-in only.
KEY_AUTO_ADD_NEW_DOORS = "auto_add_new_doors"

# Per-entry data key for caching the discovered TimeSpan write-shape.
# Hartmann's DoorTimeSpan POST/PUT field names aren't fully documented in the
# Swagger (Start/Stop are marked readOnly but the form clearly writes them).
# We probe once per entry and cache "minutes" or "strings".
KEY_TIMESPAN_SHAPE = "timespan_shape"

# DoorTimeZone mode enum from Hartmann (both Odyssey and Protector.Net).
SCHEDULE_MODES = [
    "Lockdown",
    "Card",
    "Pin",
    "CardOrPin",
    "CardAndPin",
    "Unlock",
    "UnlockWithFirstCardIn",
    "DualCard",
]

# API-enum -> friendly label (mirrors Hartmann's TimeZone editor UI).
SCHEDULE_MODE_LABELS = {
    "Lockdown":               "Lockdown",
    "Card":                   "Card Only",
    "Pin":                    "Pin Only",
    "CardOrPin":              "Card or Pin",
    "CardAndPin":             "Card and Pin",
    "Unlock":                 "Unlock",
    "UnlockWithFirstCardIn":  "First Credential In",
    "DualCard":               "Dual",
}

# Numeric TimeSpanStateVal seen in DoorTimeSpan rows.
# Mirrors TZ_INDEX_TO_FRIENDLY but maps to API enum names instead of friendly.
SCHEDULE_MODE_TO_VAL = {
    "Lockdown":              0,
    "Card":                  1,
    "Pin":                   2,
    "CardOrPin":             3,
    "CardAndPin":            4,
    "Unlock":                5,
    "UnlockWithFirstCardIn": 6,
    "DualCard":              7,
}

SCHEDULE_VAL_TO_MODE = {v: k for k, v in SCHEDULE_MODE_TO_VAL.items()}

# Default mode used when the HA TZ is first provisioned.
DEFAULT_SCHEDULE_MODE = "CardOrPin"


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

# --- Door contact binary sensors --------------------------------------------
# Per-door open/closed state derived from panel inputs configured as
# Door_Contact (or Monitored_Door_Contact). Discovered automatically from
# /api/Panels and /api/Panels/{Id}/Inputs at integration load time and
# refreshed on the hourly name-sync.

# The two InputUsage strings Hartmann uses for door-contact inputs.
# `Door_Contact` is the standard contact on a regular door (with reader/strike).
# `Monitored_Door_Contact` is the standalone monitored-door variant (no reader).
DOOR_CONTACT_USAGES = ("Door_Contact", "Monitored_Door_Contact")

# Per-entry data key (hass.data[DOMAIN][entry_id][...]) holding the
# discovered contact map. Built fresh on every load and on the hourly tick.
#   {(panel_mac, input_index): {
#       "door_id":     int,
#       "panel_id":    int,
#       "is_inverted": bool,
#       "input_name":  str,
#   }}
KEY_DOOR_CONTACT_MAP = "door_contact_map"

# Per-entry data key holding the most recent enabled value seen on each
# input that's in the contact map. Lets a freshly-added binary_sensor
# entity read the current state immediately on async_added_to_hass instead
# of waiting for the next WS transition (which may never come for a stable
# door). Keyed identically to KEY_DOOR_CONTACT_MAP.
#   {(panel_mac, input_index): bool}  # raw `enabled` from WS frame
KEY_INPUT_STATE_CACHE = "input_state_cache"

# Per-entry data key holding the most recent door open/closed/held-open state
# derived from `DOOR_CONTACT_STATE` notification frames (NotificationType =
# "DOOR_CONTACT_STATE", StateValues in {OPEN, CLOSED, HELD_OPEN}). This is the
# only state source on panels that don't push raw `Input` status frames for
# the contact — Hartmann reports door physical state purely as notifications,
# already polarity-corrected. Keyed by Hartmann door_id so the binary_sensor
# can seed itself on async_added_to_hass without scanning the contact map.
#   {door_id: {"is_open": bool, "held_open": bool, "ts": str|None}}
KEY_DOOR_CONTACT_STATE_CACHE = "door_contact_state_cache"

# Per-entry data key holding the per-door held-open threshold (in milliseconds)
# read from /api/Doors `AllowedHeldOpenTime`. Protector.Net (legacy) does NOT
# emit a `DOOR_CONTACT_STATE | HELD_OPEN` SignalR notification — its web UI
# derives the "Held Open" badge purely client-side once the door's contact has
# been ON longer than this threshold. To stay parity with Hartmann's UI on
# Protector.Net, ws.py starts a per-door asyncio timer when the contact opens
# and synthesizes a held-open dispatch when the timer elapses. Odyssey panels
# that DO send a real HELD_OPEN notification short-circuit the timer (the
# notification arrives first and updates the cache directly).
#
# Value is None when the door has `DisableHeldOpen=true` set in Hartmann —
# in that case we skip the timer entirely so we don't fight the panel's
# explicit "no held-open detection" config.
#
#   {door_id: int_ms_or_None}
KEY_DOOR_HELD_OPEN_THRESHOLDS = "door_held_open_thresholds"

# Default held-open threshold (milliseconds) used when the per-door value
# can't be fetched (transient API error, door config endpoint unavailable on
# very old Protector.Net builds). 30 seconds matches Hartmann's factory
# default for AllowedHeldOpenTime.
DEFAULT_HELD_OPEN_THRESHOLD_MS = 30000

# Per-entry data key holding the last-seen merged door status payload for
# each door. Populated by ws.py on every door frame (merging non-None fields
# so a partial frame can't overwrite a previously good strike/opener with
# None). Read by __init__.py's post-setup task to re-dispatch states once
# entities are guaranteed to be subscribed — covers the WS-burst-vs-entity-
# subscribe timing race that otherwise leaves sensors at "Unknown" on
# Protector.Net (which has no REST snapshot fallback like Odyssey).
#
#   {door_id: {"strike": bool|None, "opener": bool|None,
#              "overridden": bool|None, "timeZone": int|None}}
KEY_LAST_DOOR_STATUS = "last_door_status"

# How to interpret the `enabled` field on Input WS frames relative to the
# input's `IsInverted` config.
#   "raw"        → WS frame carries the raw circuit state; we apply IsInverted
#                  ourselves to derive open/closed.
#   "preapplied" → Hartmann pre-applies IsInverted before sending, so we use
#                  `enabled` directly as the logical state.
#
# Default is "raw" (XOR with IsInverted), based on standard alarm-industry
# convention. If this turns out to be wrong on real hardware, flipping this
# single constant fixes every door.
#
# When in doubt, the `raw_enabled` and `is_inverted` attributes on each
# binary_sensor expose the underlying values so the convention can be
# verified against physical state.
INVERSION_CONVENTION = "raw"


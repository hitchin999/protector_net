"""Per-door open/closed binary_sensor platform.

One ProtectorDoorContactSensor entity per Hartmann door, with
device_class=DOOR. Friendly name is just the door name (via
_attr_has_entity_name=True + _attr_name=None) so HA's entity pickers show:

    1st Floor Elevator Door         Binary sensor
    Basement Elevator Door          Binary sensor
    ...

State source:
    Hartmann's WebSocket sends panel input state changes as separate
    "Input" status frames (statusType="Input", statusId="<MAC>::Input::<n>",
    enabled=0|1). At integration load (and on the hourly name-sync), api.py
    discovers which (panel_mac, input_index) pairs are configured as
    Door_Contact / Monitored_Door_Contact and maps them to door_ids. ws.py
    routes incoming Input frames through that map and fires
    DISPATCH_DOOR_CONTACT, which this platform listens to.

Default state for unmonitored doors:
    Doors without a Door_Contact input configured in Hartmann default to
    Closed (off), matching Hartmann's own UI convention — every door in
    Hartmann's System Overview reads "Closed" regardless of whether a
    contact is wired. The `contact_configured` attribute exposes the
    truth (false when no contact exists), so power users who want a
    fail-loud signal can branch on it in templates/automations.

Polarity:
    Each input has an `IsInverted` flag. We apply it ourselves
    (is_open = bool(enabled) XOR is_inverted) under the assumption that
    Hartmann sends raw circuit state on the wire — controlled by
    INVERSION_CONVENTION in const.py. If real-hardware testing shows
    Hartmann pre-applies inversion, flipping that constant to "preapplied"
    fixes every door at once.

Source priority:
    Two state sources can drive this entity: raw Input status frames (XOR
    polarity path) and notifications (DOOR_CONTACT_STATE /
    DOOR_CONTACT_INPUT_STATE — already polarity-corrected by Hartmann).
    Once a notification has been seen for a door, that source becomes
    authoritative for is_on and held_open; subsequent Input frames are
    used only to refresh diagnostic attributes. This is required on
    Protector.Net which sends a 60s Input heartbeat — without this guard,
    a misconfigured IsInverted would flip the entity back to Closed every
    minute while the door is physically open, and would also clobber the
    held_open dimension which lives solely in the notification path.

Held-open detection:
    Odyssey panels emit DOOR_CONTACT_STATE | HELD_OPEN once a door has
    been open longer than its AllowedHeldOpenTime — we receive that and
    set held_open=True directly. Protector.Net does NOT emit such a
    notification (its own UI derives the badge purely client-side), so
    ws.py starts a per-door asyncio timer when the contact opens and
    synthesizes a HELD_OPEN dispatch when the threshold elapses. The
    binary_sensor handles both identically.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from . import api
from .const import (
    DOMAIN,
    INVERSION_CONVENTION,
    KEY_DOOR_CONTACT_MAP,
    KEY_DOOR_CONTACT_STATE_CACHE,
    KEY_INPUT_STATE_CACHE,
)
from .ws import DISPATCH_DOOR_CONTACT

_LOGGER = logging.getLogger(f"{DOMAIN}.binary_sensor")

# Dispatcher signal fired by __init__.py whenever the contact map is rebuilt
# (deferred-start, hourly tick). Entities listen so they can transition from
# "Unknown" to a real state the moment a contact appears in the map.
DISPATCH_CONTACT_MAP_UPDATED = f"{DOMAIN}_contact_map_updated"


def _compute_is_open(enabled: bool, is_inverted: bool) -> bool:
    """Apply the integration-wide convention to derive open/closed.

    See INVERSION_CONVENTION in const.py for the rationale.
    """
    if INVERSION_CONVENTION == "preapplied":
        return bool(enabled)
    # "raw" (default): WS carries raw circuit state, we XOR with IsInverted.
    return bool(enabled) ^ bool(is_inverted)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one ProtectorDoorContactSensor per Hartmann door.

    We deliberately create the entity for every door, regardless of whether
    a Door_Contact input is currently configured for it. Reasons:

      1. Service pickers should show every door consistently — users
         shouldn't have to wonder why one door is missing from the dropdown.
      2. Adding a contact in Hartmann after setup just means the existing
         entity transitions from Unknown to a real state on the next
         contact-map rebuild (hourly), no integration reload needed.
    """
    cfg = hass.data[DOMAIN][entry.entry_id]
    base_url: str = cfg["base_url"]

    try:
        doors = await api.get_all_doors(hass, entry.entry_id)
    except Exception as e:
        _LOGGER.error(
            "[%s] Failed to fetch doors for door-contact binary_sensors: %s",
            entry.entry_id, e,
        )
        return

    if not doors:
        _LOGGER.debug("[%s] No doors found; skipping door-contact binary_sensors", entry.entry_id)
        return

    entities = [
        ProtectorDoorContactSensor(
            hass,
            entry.entry_id,
            base_url,
            int(d["Id"]),
            str(d.get("Name") or f"Door {d['Id']}"),
        )
        for d in doors
        if "Id" in d
    ]
    if entities:
        async_add_entities(entities)
        _LOGGER.debug(
            "[%s] Added %d door-contact binary_sensor(s)",
            entry.entry_id, len(entities),
        )


class ProtectorDoorContactSensor(BinarySensorEntity, RestoreEntity):
    """Door open/closed contact for one Hartmann door.

    Friendly name is just the door name (no suffix) so this is the obvious
    primary entity per door in HA's pickers — used as the entity selector
    target by override_door, resume_door, and set_door_schedule_mode.
    """

    _attr_has_entity_name = True
    _attr_name = None  # use device name -> picker shows just the door name
    _attr_device_class = BinarySensorDeviceClass.DOOR
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        base_url: str,
        door_id: int,
        door_name: str,
    ) -> None:
        self.hass = hass
        self._entry_id = entry_id
        self._base_url = base_url
        self._door_id = int(door_id)
        self._door_name = door_name

        host = base_url.split("://", 1)[1]
        self._host = host
        self._attr_unique_id = f"{DOMAIN}_{host}_door_{door_id}_contact|{entry_id}"

        # State defaults to Closed (False) — matches Hartmann's UI convention
        # where every door reads "Closed" until a contact is wired and reports
        # otherwise. Restored state (if any) and cached WS values override
        # this on async_added_to_hass; live WS Input frames override during
        # operation. Doors with no contact configured stay at Closed until
        # one is wired up.
        #
        # Note: every HA restart logs a transition to this entity's state
        # in the recorder (unavailable -> off|on) — that's standard HA
        # behavior for every entity in every integration, not specific to
        # this binary_sensor. The "Was closed" entry users see at restart
        # timestamps is HA's recorder logging the unavailable -> off
        # transition as the entity comes back online.
        #
        # Power users who want a "fail-loud" Unknown signal for unmonitored
        # doors can branch on the `contact_configured` attribute.
        self._attr_is_on: bool = False

        # Tracked for attributes
        self._raw_enabled: Optional[bool] = None
        self._is_inverted: Optional[bool] = None
        self._panel_mac: Optional[str] = None
        self._input_idx: Optional[int] = None
        self._input_name: Optional[str] = None
        self._last_changed: Optional[str] = None
        # Held-open state from DOOR_CONTACT_STATE notifications. None means
        # we have never received a notification for this door (e.g. the
        # entity is fresh and Hartmann hasn't sent a frame yet) — distinct
        # from False ("not currently held open"). Notification path keeps
        # this in sync with the door's true open/held-open/closed state.
        self._held_open: Optional[bool] = None
        # Tracks whether we've ever seen a DOOR_CONTACT_STATE notification
        # for this door — if so, the door definitively has a contact wired,
        # even if build_door_contact_map didn't find an Input mapping.
        self._notif_contact_seen: bool = False

        self._unsub_event: Optional[Callable[[], None]] = None
        self._unsub_map: Optional[Callable[[], None]] = None

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"door:{self._host}:{self._door_id}|{self._entry_id}")},
            "manufacturer": "Yoel Goldstein/Vaayer LLC",
            "model": "Protector.Net Door",
            "name": self._door_name,
            "configuration_url": self._base_url,
            "via_device": (DOMAIN, f"hub:{self._host}|{self._entry_id}"),
        }

    @property
    def available(self) -> bool:
        # Stay "available" even when no contact is configured — the entity
        # is a valid service target regardless. State just stays Unknown.
        # Mirrors how other per-door sensors behave when their data is missing.
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        info = self._lookup_contact_info()
        # contact_configured is true if either the input-status mapping was
        # discovered (Hartmann admin: InputUsage = Door_Contact /
        # Monitored_Door_Contact) OR we've ever received a
        # DOOR_CONTACT_STATE notification for this door (definitive proof
        # the door has a physical contact wired).
        return {
            "panel_mac":           self._panel_mac,
            "input_index":         self._input_idx,
            "input_name":          self._input_name,
            "is_inverted":         self._is_inverted,
            "raw_enabled":         self._raw_enabled,
            "contact_configured":  (info is not None) or self._notif_contact_seen,
            "inversion_convention": INVERSION_CONVENTION,
            "last_changed":        self._last_changed,
            # Held-open: true while the door has been open longer than the
            # panel's configured Held-Open threshold, false once it closes
            # (or once we receive a fresh OPEN/CLOSED notification). None
            # until the first DOOR_CONTACT_STATE notification arrives.
            "held_open":           self._held_open,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # 1) Restore last known state so the entity doesn't show Unknown
        #    across HA restarts when nothing has changed at the panel.
        last = await self.async_get_last_state()
        if last and last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE, None, ""):
            if last.state == "on":
                self._attr_is_on = True
            elif last.state == "off":
                self._attr_is_on = False
            la = last.attributes or {}
            self._raw_enabled  = la.get("raw_enabled")
            self._is_inverted  = la.get("is_inverted")
            self._panel_mac    = la.get("panel_mac")
            self._input_idx    = la.get("input_index")
            self._input_name   = la.get("input_name")
            self._last_changed = la.get("last_changed")
            # Restore held_open across HA restart. Note: this is a
            # best-effort restore — if the door changed state while HA
            # was down, the next DOOR_CONTACT_STATE notification will
            # correct it.
            ho = la.get("held_open")
            if isinstance(ho, bool):
                self._held_open = ho

        # 2) If a fresher value was cached by ws.py since restore, prefer it.
        info = self._lookup_contact_info()
        if info is not None:
            self._is_inverted = bool(info.get("is_inverted"))
            self._input_name  = info.get("input_name")
            # _lookup_contact_info populated self._panel_mac and self._input_idx
            # as a side effect; use them directly for the cache lookup.
            cache = (self.hass.data.get(DOMAIN, {})
                     .get(self._entry_id, {}).get(KEY_INPUT_STATE_CACHE) or {})
            cached = cache.get((self._panel_mac, self._input_idx))
            if cached is not None:
                self._raw_enabled = bool(cached)
                self._attr_is_on  = _compute_is_open(
                    bool(cached), bool(info.get("is_inverted")),
                )
                self._last_changed = dt_util.utcnow().isoformat(timespec="seconds")

        # 2b) Seed from DOOR_CONTACT_STATE notification cache. This wins
        #     over the input-status path when both have a value, because
        #     Hartmann's notification is logical state (already polarity-
        #     corrected) — the canonical answer to "is the door open?".
        ds_cache = (self.hass.data.get(DOMAIN, {})
                    .get(self._entry_id, {})
                    .get(KEY_DOOR_CONTACT_STATE_CACHE) or {})
        ds_cached = ds_cache.get(self._door_id)
        if isinstance(ds_cached, dict):
            self._attr_is_on   = bool(ds_cached.get("is_open"))
            self._held_open    = bool(ds_cached.get("held_open"))
            self._last_changed = (ds_cached.get("ts")
                                  or dt_util.utcnow().isoformat(timespec="seconds"))
            self._notif_contact_seen = True

        # 3) Subscribe to live contact events.
        self._unsub_event = async_dispatcher_connect(
            self.hass,
            f"{DISPATCH_DOOR_CONTACT}_{self._entry_id}",
            self._handle_contact_event,
        )

        # 4) Subscribe to contact-map rebuild events so we can pick up a
        #    newly-added contact without an integration reload.
        self._unsub_map = async_dispatcher_connect(
            self.hass,
            f"{DISPATCH_CONTACT_MAP_UPDATED}_{self._entry_id}",
            self._handle_map_updated,
        )

        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_event:
            self._unsub_event()
            self._unsub_event = None
        if self._unsub_map:
            self._unsub_map()
            self._unsub_map = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _lookup_contact_info(self) -> Optional[dict[str, Any]]:
        """Return the contact-map entry for this door, with panel_mac+input_idx
        injected. None if no contact is configured for this door.

        Side effect: caches `panel_mac` / `input_idx` on the entity so other
        methods (and the attributes property) don't have to scan the map.
        """
        cmap = (self.hass.data.get(DOMAIN, {})
                .get(self._entry_id, {}).get(KEY_DOOR_CONTACT_MAP) or {})
        for (mac, idx), info in cmap.items():
            if int(info.get("door_id", -1)) == self._door_id:
                self._panel_mac = mac
                self._input_idx = idx
                return {**info, "panel_mac": mac, "input_idx": idx}
        return None

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    @callback
    def _handle_contact_event(self, evt: dict[str, Any]) -> None:
        if int(evt.get("door_id", -1)) != self._door_id:
            return

        # Two event shapes converge on this dispatcher:
        #   (a) "input"       — raw Input status frame (ws.py status branch).
        #                       Carries `enabled` (0/1) + `is_inverted`. We
        #                       XOR per INVERSION_CONVENTION to get is_open.
        #   (b) "notification"— DOOR_CONTACT_STATE notification (ws.py notif
        #                       branch). Carries `is_open` (already logical)
        #                       and `held_open`. This is the only state
        #                       source on panels that don't push raw Input
        #                       frames for door contacts.
        #
        # Notification path wins: it's polarity-corrected by Hartmann and
        # carries the held-open dimension that the input path can't.
        source = evt.get("source")
        if source == "notification":
            self._notif_contact_seen = True
            new_state = bool(evt.get("is_open"))
            new_held  = bool(evt.get("held_open"))

            ts = evt.get("ts") or dt_util.utcnow().isoformat(timespec="seconds")
            changed = False
            if self._attr_is_on != new_state:
                self._attr_is_on = new_state
                changed = True
            if self._held_open != new_held:
                self._held_open = new_held
                changed = True
            if changed:
                self._last_changed = ts

            self.async_write_ha_state()
            return

        # --- input-status path (legacy) ---
        # We still update raw_enabled / is_inverted / panel_mac / input_idx
        # so the diagnostic attributes always reflect the latest panel
        # heartbeat, even when we choose not to drive `is_on` from it.
        enabled    = bool(evt.get("enabled"))
        is_invert  = bool(evt.get("is_inverted"))
        new_state  = _compute_is_open(enabled, is_invert)

        self._raw_enabled  = enabled
        self._is_inverted  = is_invert
        self._panel_mac    = evt.get("panel_mac")
        self._input_idx    = evt.get("input_idx")
        # Keep input_name in sync if the map has it (it does; pull from there)
        info = self._lookup_contact_info()
        if info:
            self._input_name = info.get("input_name")

        # Notification-path authoritative once we've seen one. Why:
        # Protector.Net pushes a periodic Input baseline every ~60s carrying
        # the input's raw `enabled` value. If the input's IsInverted flag is
        # mis-set in Hartmann (or our contact map), the XOR computation
        # disagrees with Hartmann's logical state — the panel says door is
        # open, but our XOR says closed, and every minute the heartbeat
        # would flip the binary_sensor back to "Closed" while the door is
        # still physically open. Hartmann's notifications (DOOR_CONTACT_STATE
        # and DOOR_CONTACT_INPUT_STATE) carry the LOGICAL state already
        # polarity-corrected by the panel, so once we've seen one, we
        # treat that source as canonical and ignore raw Input frames for
        # is_on. This also protects held_open from being clobbered when
        # the periodic heartbeat re-asserts an "open" Input the user is
        # still physically holding — the held_open dimension lives only
        # in the notification path and would otherwise get cleared.
        #
        # Doors that ONLY receive Input frames (no DOOR_CONTACT_STATE ever
        # arrives — old hardware, unusual config) keep the legacy behavior:
        # _notif_contact_seen stays False, so this branch still drives
        # is_on. They just can't get held_open detection without a notif.
        if self._notif_contact_seen:
            # Only refresh diagnostic attributes — leave is_on/held_open
            # to the notification path which fired (or will fire) the
            # authoritative state.
            self.async_write_ha_state()
            return

        if self._attr_is_on != new_state:
            self._attr_is_on = new_state
            self._last_changed = dt_util.utcnow().isoformat(timespec="seconds")
            # Input-path closing transitions clear held_open; opens leave it
            # alone (the held_open trip arrives later as its own notification).
            if not new_state and self._held_open:
                self._held_open = False

        self.async_write_ha_state()

    @callback
    def _handle_map_updated(self, contact_map: dict) -> None:
        """Re-evaluate when the contact map is rebuilt.

        Fires on every hourly sync and after a config-flow change. If a
        contact was just added/removed for this door, we want the entity's
        attributes (and possibly state) to reflect that immediately.
        """
        info = self._lookup_contact_info()
        if info is None:
            # Contact removed (or never existed). Clear inversion/index data
            # but leave is_on alone — last known state is still useful.
            self._is_inverted = None
            self._panel_mac   = None
            self._input_idx   = None
            self._input_name  = None
            self.async_write_ha_state()
            return

        self._is_inverted = bool(info.get("is_inverted"))
        self._input_name  = info.get("input_name")

        # If a cached enabled value exists for the now-mapped input, use it.
        cache = self.hass.data.get(DOMAIN, {}).get(self._entry_id, {}).get(KEY_INPUT_STATE_CACHE) or {}
        cached = cache.get((info["panel_mac"], info["input_idx"]))
        if cached is not None:
            self._raw_enabled = bool(cached)
            new_state = _compute_is_open(bool(cached), bool(info.get("is_inverted")))
            if self._attr_is_on != new_state:
                self._attr_is_on = new_state
                self._last_changed = dt_util.utcnow().isoformat(timespec="seconds")

        self.async_write_ha_state()

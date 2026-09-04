"""Per-door override intent, and restoring overrides a panel forgot.

An override lives only in the panel's RAM. If the panel loses power — or is
rebooted by an action plan on every Update Panels, as one site does — it comes
back following its schedule and the override is simply gone. Hartmann does not
announce that the override ended: it announces a resume only when one ends
deliberately, so a lost override is silent.

This module holds the *desired* state. The panel holds the actual state, and a
reconciler closes the gap after a cold start.

The rule that makes it safe: **intent is recorded on user action, and written
before the command is sent — never on command success.** If someone presses
Resume while the panel is offline the command never lands, but intent still
flips to "should not be overridden", so the door is left alone when the panel
returns. Recording intent on success would leave the old override in place and
re-apply it against the user's wishes.

That ordering also makes the whole thing race-free against the websocket. A
user-initiated resume and a panel reboot both end with the door following its
schedule; the only thing that distinguishes them is that a deliberate resume
cleared intent first.

Deliberately *not* restored: "Until Next Schedule". A cold-starting panel
reloads its schedule, which is arguably itself the next schedule event, so
that override type has no reboot-independent meaning. (Confirmed on hardware:
pushing a schedule change ends an Until Next Schedule override immediately,
while Until Resumed survives it.)
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import timedelta
from typing import Any, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    DOMAIN,
    KEY_LAST_DOOR_STATUS,
    KEY_OVERRIDE_INTENT,
    SIGNAL_OVERRIDE_INTENT,
)

_LOGGER = logging.getLogger(f"{DOMAIN}.override_intent")

STORE_VERSION = 1

# Wait after a panel reports STARTED before reconciling. The panel's own door
# notifications arrive milliseconds after the panel-status frame, and we want
# the cached door state to reflect the post-reboot reality before deciding
# whether an override is actually missing.
RESTORE_SETTLE_SECONDS = 5.0

# Override types we can put back. "NextSchedule" is intentionally absent.
RESTORABLE_TYPES = ("Resume", "Time")


def _store_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}.override_intent"


class OverrideIntentStore:
    """Remembers, per door, whether it *should* currently be overridden."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.entry_id = entry.entry_id
        self._store: Store = Store(hass, STORE_VERSION, _store_key(entry.entry_id))
        self._doors: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._loaded = False

    # ---------------- persistence ----------------

    async def async_load(self) -> None:
        try:
            data = await self._store.async_load()
        except Exception as e:  # never block setup on a bad store
            _LOGGER.warning("[%s] Could not load override intent: %s", self.entry_id, e)
            data = None
        self._doors = dict((data or {}).get("doors") or {})
        self._loaded = True
        active = [d for d, s in self._doors.items() if s.get("should_be_overridden")]
        if active:
            _LOGGER.debug(
                "[%s] Loaded override intent; doors expecting an override: %s",
                self.entry_id, active,
            )

    async def _async_save(self) -> None:
        # Immediate write, not delayed: intent changes are rare, and a delayed
        # write is exactly what a power cut would lose — the scenario this
        # feature exists for.
        try:
            await self._store.async_save({"doors": self._doors})
        except Exception as e:
            _LOGGER.warning("[%s] Could not save override intent: %s", self.entry_id, e)
        # Tell the Restore Override switches to re-render. They don't poll, so
        # without this an override applied from anywhere else (the Override
        # switch, a service call, the card) would leave their attributes
        # showing whatever was true when they last wrote state.
        async_dispatcher_send(self.hass, f"{SIGNAL_OVERRIDE_INTENT}_{self.entry_id}")

    async def async_remove(self) -> None:
        try:
            await self._store.async_remove()
        except Exception:
            pass

    # ---------------- accessors ----------------

    @callback
    def get(self, door_id: int) -> dict[str, Any]:
        return dict(self._doors.get(str(door_id)) or {})

    @callback
    def is_enabled(self, door_id: int) -> bool:
        return bool((self._doors.get(str(door_id)) or {}).get("enabled"))

    @callback
    def any_enabled(self) -> bool:
        return any(s.get("enabled") for s in self._doors.values())

    async def async_set_enabled(self, door_id: int, enabled: bool) -> None:
        st = self._doors.setdefault(str(door_id), {})
        st["enabled"] = bool(enabled)
        if not enabled:
            # Turning the switch off forgets what we were holding, so flipping
            # it back on later can't resurrect a stale override.
            st.pop("should_be_overridden", None)
            st.pop("mode", None)
            st.pop("override_type", None)
            st.pop("ends_at", None)
            st.pop("overridden_since", None)
        await self._async_save()

    async def async_record_override(
        self,
        door_id: int,
        *,
        mode: Optional[str],
        override_type: Optional[str],
        minutes: Optional[int] = None,
    ) -> None:
        """Record that this door should be overridden. Call BEFORE the command."""
        st = self._doors.setdefault(str(door_id), {})
        now = dt_util.utcnow()
        st["should_be_overridden"] = True
        st["mode"] = mode
        st["override_type"] = override_type
        # Store an absolute end time, never a duration: after a reboot of
        # unknown length "45 minutes remaining" is meaningless, but a
        # timestamp still is.
        if override_type == "Time" and minutes:
            st["ends_at"] = (now + timedelta(minutes=int(minutes))).isoformat()
        else:
            st["ends_at"] = None
        st.setdefault("overridden_since", now.isoformat())
        await self._async_save()

    async def async_clear(self, door_id: int) -> None:
        """Record that this door should NOT be overridden. Call BEFORE the command."""
        st = self._doors.setdefault(str(door_id), {})
        if not st.get("should_be_overridden"):
            return
        st["should_be_overridden"] = False
        st["mode"] = None
        st["override_type"] = None
        st["ends_at"] = None
        st["overridden_since"] = None
        await self._async_save()

    async def async_prune(self, valid_door_ids: set[int]) -> None:
        """Drop intent for doors that no longer exist in Hartmann."""
        stale = [d for d in self._doors if int(d) not in valid_door_ids]
        if not stale:
            return
        for d in stale:
            self._doors.pop(d, None)
        await self._async_save()

    # ---------------- reconciliation ----------------

    @callback
    def _cached_status(self, door_id: int) -> dict[str, Any]:
        cache = (self.hass.data.get(DOMAIN, {})
                 .get(self.entry_id, {})
                 .get(KEY_LAST_DOOR_STATUS)) or {}
        return dict(cache.get(door_id) or {})

    async def async_reconcile(self, reason: str = "panel started") -> None:
        """Put back any override a panel forgot.

        Correctness comes from the intent gate, not from the state check — the
        state check only avoids a redundant panel command. If cached state is
        stale or unknown we re-apply, because the door ending up overridden is
        what intent asked for either way.
        """
        if not self._loaded:
            return
        from . import api  # local import keeps this module import-light

        async with self._lock:
            await asyncio.sleep(RESTORE_SETTLE_SECONDS)

            now = dt_util.utcnow()
            for door_key, st in list(self._doors.items()):
                if not st.get("enabled") or not st.get("should_be_overridden"):
                    continue
                try:
                    door_id = int(door_key)
                except (TypeError, ValueError):
                    continue

                otype = st.get("override_type")
                if otype not in RESTORABLE_TYPES:
                    continue

                minutes: Optional[int] = None
                if otype == "Time":
                    ends_at = st.get("ends_at")
                    if not ends_at:
                        continue
                    end_dt = dt_util.parse_datetime(ends_at)
                    if end_dt is None:
                        continue
                    remaining = (end_dt - now).total_seconds()
                    if remaining <= 0:
                        _LOGGER.debug(
                            "[%s] Door %s override window elapsed while the panel "
                            "was down; not restoring", self.entry_id, door_id,
                        )
                        await self.async_clear(door_id)
                        continue
                    minutes = max(1, math.ceil(remaining / 60))

                if self._cached_status(door_id).get("overridden") is True:
                    continue  # still overridden; nothing was lost

                mode = st.get("mode")
                _LOGGER.info(
                    "[%s] Restoring override on door %s (%s / %s%s) after %s",
                    self.entry_id, door_id, mode, otype,
                    f" / {minutes}min" if minutes else "", reason,
                )
                try:
                    ok = await api.apply_override(
                        self.hass, self.entry_id, [door_id],
                        override_type=otype, mode=mode, minutes=minutes,
                    )
                except Exception as e:
                    _LOGGER.warning(
                        "[%s] Override restore failed for door %s: %s",
                        self.entry_id, door_id, e,
                    )
                    continue

                if not ok:
                    _LOGGER.warning(
                        "[%s] Override restore rejected for door %s", self.entry_id, door_id
                    )
                    continue

                st["last_restored_at"] = dt_util.utcnow().isoformat()
                st["restore_count"] = int(st.get("restore_count") or 0) + 1
                await self._async_save()


@callback
def async_get_intent(hass: HomeAssistant, entry_id: str) -> Optional[OverrideIntentStore]:
    """Return this entry's intent store, or None if it isn't loaded."""
    return (hass.data.get(DOMAIN, {}).get(entry_id, {}) or {}).get(KEY_OVERRIDE_INTENT)


async def async_record_override(
    hass: HomeAssistant,
    entry_id: str,
    door_ids: list[int],
    *,
    mode: Optional[str],
    override_type: Optional[str],
    minutes: Optional[int] = None,
) -> None:
    """Record intent for doors whose Restore Override switch is on. Best-effort."""
    store = async_get_intent(hass, entry_id)
    if store is None:
        return
    for did in door_ids:
        try:
            if store.is_enabled(int(did)):
                await store.async_record_override(
                    int(did), mode=mode, override_type=override_type, minutes=minutes,
                )
        except Exception as e:
            _LOGGER.debug("[%s] Could not record override intent for %s: %s", entry_id, did, e)


async def async_clear_override(
    hass: HomeAssistant, entry_id: str, door_ids: list[int]
) -> None:
    """Clear intent for the given doors. Best-effort, and never raises."""
    store = async_get_intent(hass, entry_id)
    if store is None:
        return
    for did in door_ids:
        try:
            await store.async_clear(int(did))
        except Exception as e:
            _LOGGER.debug("[%s] Could not clear override intent for %s: %s", entry_id, did, e)


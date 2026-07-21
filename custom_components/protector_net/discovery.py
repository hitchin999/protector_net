# custom_components/protector_net/discovery.py
"""Self-heal helpers: re-create door entities after a Hartmann outage.

Every door platform (select / number / switch / binary_sensor / sensor)
enumerates doors with a live REST call at setup time. If Hartmann is
unreachable at that moment (server reboot, network blip, the 4am panel
bounce, etc.) the fetch fails, the platform creates zero door entities, and
setup still completes "successfully" — so the pre-existing RestoreEntity
registry rows go **unavailable** with no backing object, and nothing ever
retries. Automations keyed on those entities silently stop firing.

The WS client already detects recovery: its reconnect loop keeps trying and,
on success, fires SIGNAL_HUB_CONNECTED. These helpers let each platform hang
a *backfill* off that signal — re-enumerate doors and add any that aren't
present yet. On a healthy boot every door is already tracked, so the backfill
is a no-op; it only does work after a setup-time outage. This is what makes
the doors come back on their own once Hartmann is up again.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, SIGNAL_HUB_CONNECTED

_LOGGER = logging.getLogger(DOMAIN)


@callback
def async_on_hub_connected(
    hass: HomeAssistant,
    entry: ConfigEntry,
    handler: Callable[..., Coroutine[Any, Any, None]],
) -> Callable[[], None]:
    """Run ``handler`` (an async fn) on every successful WS (re)connect.

    Registers a dispatcher listener (auto-removed on unload). Additionally, if
    the hub is *already* connected at the time we subscribe — i.e. we
    registered after the connect transition already fired — we kick one
    immediate pass so we don't have to wait for the next reconnect. Handlers
    are expected to be idempotent (no-op when nothing new to add), so the
    extra pass is harmless on a healthy boot.
    """
    entry_id = entry.entry_id

    unsub = async_dispatcher_connect(
        hass, f"{SIGNAL_HUB_CONNECTED}_{entry_id}", handler
    )
    entry.async_on_unload(unsub)

    hub = (hass.data.get(DOMAIN, {}).get(entry_id, {}) or {}).get("hub")
    if hub is not None and getattr(hub, "connected", False):
        hass.async_create_task(handler())

    return unsub


@callback
def async_setup_door_platform_backfill(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    added_door_ids: set[int],
    add_doors: Callable[[list[dict]], None],
    label: str,
) -> None:
    """Register self-heal backfill for a platform whose doors come straight
    from ``api.get_all_doors`` (select / number / switch / binary_sensor).

    ``added_door_ids`` is the shared set of door IDs the platform has already
    created entities for; the caller MUST populate it during its initial add
    so the backfill knows what's missing. ``add_doors(new_door_dicts)`` builds
    AND adds the entities for the given (already-filtered-to-new) doors, using
    whatever ``async_add_entities`` kwargs that platform needs; this helper
    then records their IDs. A lock serialises concurrent passes (a reconnect
    firing while the immediate pass is still running) so a door can't be
    double-added across the ``await``.
    """
    # Imported lazily to keep this module free of a hard api import at load.
    from . import api

    entry_id = entry.entry_id
    lock = asyncio.Lock()

    async def _backfill(*_args: Any) -> None:
        async with lock:
            try:
                doors = await api.get_all_doors(hass, entry_id)
            except Exception as e:  # never let a backfill attempt raise
                _LOGGER.debug(
                    "[%s] %s door backfill fetch failed (will retry on next "
                    "reconnect): %s", entry_id, label, e,
                )
                return

            new = [
                d for d in (doors or [])
                if "Id" in d and int(d["Id"]) not in added_door_ids
            ]
            if not new:
                return

            add_doors(new)
            added_door_ids.update(int(d["Id"]) for d in new)
            _LOGGER.info(
                "[%s] Self-heal: backfilled %d %s door(s) after hub reconnect: %s",
                entry_id, len(new), label, [int(d["Id"]) for d in new],
            )

    async_on_hub_connected(hass, entry, _backfill)

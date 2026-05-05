# custom_components/protector_net/__init__.py

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, UI_STATE, KEY_AUTO_ADD_NEW_DOORS
from .ws import SignalRClient
from . import api
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(DOMAIN)

# Cadence for the background partition/door name sync.
# Renames are an admin event, not a hot path — hourly is invisible load-wise
# and means HA never lags more than an hour behind a Hartmann rename.
NAME_SYNC_INTERVAL = timedelta(hours=1)

# Platforms we expose
# (Old per-action buttons are going away except Pulse Unlock; new controls live in select/number/switch.)
PLATFORMS: list[str] = ["button", "sensor", "select", "number", "switch", "datetime"]


async def _sync_names_from_hartmann(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    allow_state_changes: bool = True,
) -> None:
    """Pull current partition + door names from Hartmann and update HA to match.

    Runs on every integration load (deferred start). Best-effort — a failure
    here never blocks setup. Skips devices the user has manually renamed
    (`name_by_user`) so we don't clobber their preferences.

    Why this exists: partition / door names are captured at setup time, so
    renames done in Hartmann afterward never reach HA without this sync.

    The `allow_state_changes` flag controls whether auto-add runs. We pass
    False during initial deferred-start to avoid an options write triggering
    a reload that races with in-flight platform setup; the first hourly tick
    runs with the flag True instead.
    """
    from homeassistant.helpers import device_registry as dr

    entry_id = entry.entry_id
    cfg = hass.data[DOMAIN][entry_id]
    base_url = cfg.get("base_url", "")
    host = base_url.split("://", 1)[1] if "://" in base_url else base_url

    # --- Partition name → entry title -------------------------------------
    # Entry title format is "{host} – {partition_name}"; downstream entities
    # parse the partition name out of entry.title at platform-setup time, so
    # updating the title BEFORE async_forward_entry_setups makes the freshly
    # constructed hub device pick up the new name automatically.
    try:
        part_name = await api.get_partition_name(hass, entry_id)
        if part_name:
            new_title = f"{host} – {part_name}"
            if entry.title != new_title:
                _LOGGER.info(
                    "[%s] Partition renamed in Hartmann: %r → %r",
                    entry_id, entry.title, new_title,
                )
                hass.config_entries.async_update_entry(entry, title=new_title)

                # Also patch the hub device's display name in the registry.
                # On the very first load, the hub device doesn't exist yet —
                # platforms will create it shortly with the right name from
                # the just-updated title. On later loads, the device exists
                # and needs an explicit registry update because we suppress
                # reloads for title-only changes (see _async_update_listener).
                try:
                    device_reg = dr.async_get(hass)
                    hub_ident = (DOMAIN, cfg.get("hub_identifier") or f"hub:{host}|{entry_id}")
                    hub_device = device_reg.async_get_device(identifiers={hub_ident})
                    if hub_device and not hub_device.name_by_user:
                        new_hub_name = f"Hub Status – {part_name}"
                        if hub_device.name != new_hub_name:
                            device_reg.async_update_device(hub_device.id, name=new_hub_name)
                except Exception as e:
                    _LOGGER.debug("[%s] Hub device name update skipped: %s", entry_id, e)
    except Exception as e:
        _LOGGER.debug("[%s] partition-name sync skipped: %s", entry_id, e)

    # --- Door names → device registry -------------------------------------
    # On the first integration load, no door devices exist yet — platforms
    # will create them shortly with the right name straight from the API,
    # so the name-sync loop is a no-op then. On later loads, we patch any drift.
    #
    # This block also handles auto-add: if the user opted into "auto-add new
    # doors" in Door Time Zones options, any doors here that aren't yet in
    # managed_doors get provisioned + activated automatically.
    try:
        doors = await api.get_all_doors(hass, entry_id)
        if not doors:
            return

        device_reg = dr.async_get(hass)
        host_key = cfg.get("host") or host
        renamed = 0

        for d in doors:
            door_id = d.get("Id")
            new_name = d.get("Name")
            if not door_id or not new_name:
                continue

            ident = (DOMAIN, f"door:{host_key}:{door_id}|{entry_id}")
            device = device_reg.async_get_device(identifiers={ident})
            if device is None:
                continue  # not yet created — first run

            # Don't clobber a name the user explicitly set in HA's UI.
            if device.name_by_user:
                continue

            if device.name != new_name:
                _LOGGER.info(
                    "[%s] Door %s renamed in Hartmann: %r → %r",
                    entry_id, door_id, device.name, new_name,
                )
                device_reg.async_update_device(device.id, name=new_name)
                renamed += 1

        if renamed:
            _LOGGER.debug("[%s] Door name sync updated %d device(s)", entry_id, renamed)

        # --- Auto-add new doors (opt-in) ----------------------------------
        # Compares the live Hartmann door list against the saved managed_doors
        # state. Anything present in Hartmann but not in managed_doors gets
        # provisioned + activated. Doors removed from Hartmann are intentionally
        # NOT touched here — that's a deletion event and deserves explicit
        # handling, not silent unprovisioning.
        #
        # Skipped during initial deferred-start (allow_state_changes=False) to
        # avoid the options write here triggering a reload that races with
        # forward_entry_setups. Runs normally on the hourly periodic sync.
        if allow_state_changes and entry.options.get(KEY_AUTO_ADD_NEW_DOORS, False):
            managed_now = entry.options.get("managed_doors") or {}
            current_managed_ids = {int(k) for k in managed_now.keys()}
            current_active_ids  = {int(k) for k, v in managed_now.items() if v.get("active")}

            live_door_ids: list[int] = []
            live_door_names: dict[int, str] = {}
            for d in doors:
                did = d.get("Id")
                if not did:
                    continue
                try:
                    did_int = int(did)
                except (TypeError, ValueError):
                    continue
                live_door_ids.append(did_int)
                live_door_names[did_int] = str(d.get("Name") or f"Door {did_int}")

            new_door_ids = [d for d in live_door_ids if d not in current_managed_ids]

            if new_door_ids:
                _LOGGER.info(
                    "[%s] Auto-add: %d new door(s) found in Hartmann: %s",
                    entry_id, len(new_door_ids),
                    [live_door_names[d] for d in new_door_ids],
                )

                # Desired state = existing managed/active PLUS the new doors,
                # both managed AND active (the auto-add toggle activates them).
                desired_managed = sorted(current_managed_ids | set(new_door_ids))
                desired_active  = sorted(current_active_ids  | set(new_door_ids))

                from . import managed_schedules
                summary = await managed_schedules.reconcile(
                    hass,
                    entry,
                    desired_managed_door_ids=desired_managed,
                    desired_active_door_ids=desired_active,
                    door_names=live_door_names,
                )
                _LOGGER.info(
                    "[%s] Auto-add reconcile: provisioned=%s activated=%s "
                    "deactivated=%s unprovisioned=%s failed=%s",
                    entry_id,
                    summary.get("provisioned"), summary.get("activated"),
                    summary.get("deactivated"), summary.get("unprovisioned"),
                    summary.get("failed"),
                )

                # Persist the new managed_doors map alongside existing options.
                new_options = dict(entry.options)
                new_options["managed_doors"] = summary.get("managed_doors", {})
                hass.config_entries.async_update_entry(entry, options=new_options)
    except Exception as e:
        _LOGGER.debug("[%s] door-name sync skipped: %s", entry_id, e)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration (domain) once."""
    hass.data.setdefault(DOMAIN, {})
    
    # Register services (only once per domain)
    await async_setup_services(hass)
    
    _LOGGER.debug("async_setup for %s initialized", DOMAIN)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Register a single config entry WITHOUT blocking HA startup."""
    base_url: str = entry.data["base_url"]
    host = base_url.split("://", 1)[1]

    # Persist runtime config for this entry_id (no I/O here)
    data: dict[str, Any] = {
        "base_url": base_url,
        "username": entry.data["username"],
        "password": entry.data["password"],
        "session_cookie": entry.data["session_cookie"],
        "partition_id": entry.data["partition_id"],
        "host": host,
        "hub_identifier": f"hub:{host}|{entry.entry_id}",
        "verify_ssl": bool(entry.options.get("verify_ssl", False)),
        "override_minutes": entry.options.get(
            "override_minutes", entry.data.get("override_minutes")
        ),
        # New: shared, ephemeral per-door UI state used by select/number/switch
        UI_STATE: {},  # {door_id: {"type": str, "mode": str, "minutes": int}}
        # New: cached legend for DoorTimeZoneMode to sync Override Mode select from WS
        "tz_index_to_name": {},   # {int: "Card or Pin", ...} (normalized by us)
        "tz_name_to_index": {},   # {"card or pin": 3, ...}   (normalized key)
        # Snapshot of options at load time. The update listener compares
        # against this to decide whether an entry update is a real options
        # change (reload needed) or just a title rename (no reload — would
        # cause a race with in-flight platform setup).
        "_last_options_seen": dict(entry.options),
    }
    hass.data[DOMAIN][entry.entry_id] = data

    # Make options changes trigger a reload
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    async def _deferred_start(_event=None) -> None:
        """Actually start hub + platforms after HA has started."""
        # Cache the DoorTimeZoneMode legend (best effort; we’ll retry later if needed)
        try:
            tz_map = await api.get_door_time_zone_states(hass, entry.entry_id)
            # Normalize names to user-friendly form & lowercase keys for reverse map
            name_by_idx: dict[int, str] = {}
            idx_by_name: dict[str, int] = {}
            for idx, item in tz_map.items():
                raw = str(item.get("name") or "")
                # Normalize common variants produced by servers (“CardOrPin”, “Card Or Pin”, etc.)
                nice = (
                    raw.replace("And", "and")
                       .replace("Or", "or")
                       .replace("Credential", "Credential")
                       .strip()
                )
                # Guard: ensure Unlock capitalization matches our UI
                if nice.lower() == "unlock":
                    nice = "Unlock"
                name_by_idx[int(idx)] = nice
                idx_by_name[nice.lower()] = int(idx)

            hass.data[DOMAIN][entry.entry_id]["tz_index_to_name"] = name_by_idx
            hass.data[DOMAIN][entry.entry_id]["tz_name_to_index"] = idx_by_name
            _LOGGER.debug("[%s] Loaded DoorTimeZoneMode legend: %s", entry.entry_id, name_by_idx)
        except Exception as e:
            _LOGGER.debug("[%s] Could not load DoorTimeZoneMode legend yet: %s", entry.entry_id, e)

        # Start SignalR hub (non-blocking)
        hub = SignalRClient(hass, entry.entry_id)
        hass.data[DOMAIN][entry.entry_id]["hub"] = hub
        hub.async_start()
        _LOGGER.debug("[%s] Hub started for %s", entry.entry_id, host)

        # Pull current partition + door names from Hartmann before platforms
        # build entities. The entry title is the source of truth for the
        # partition name in entity device_info, so updating it here means
        # the hub device gets the right name immediately (no second reload).
        # `allow_state_changes=False` here: auto-add can write options, and
        # that would trigger a reload race with forward_entry_setups below.
        # First hourly tick runs the full sync (auto-add included).
        await _sync_names_from_hartmann(hass, entry, allow_state_changes=False)

        # Schedule a recurring background sync so renames in Hartmann reach
        # HA without a manual reload. Cadence is intentionally low — names
        # change rarely, and the panels-online poller already runs every 60s,
        # so an hourly call is a rounding error on top of that.
        async def _periodic_name_sync(_now) -> None:
            await _sync_names_from_hartmann(hass, entry, allow_state_changes=True)

        cancel_sync = async_track_time_interval(
            hass, _periodic_name_sync, NAME_SYNC_INTERVAL
        )
        entry.async_on_unload(cancel_sync)

        # Now set up platforms (these return quickly; our platforms offload I/O)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        _LOGGER.debug("[%s] Platforms set up: %s", entry.entry_id, ", ".join(PLATFORMS))

    # If HA already running (entry added later), start immediately; otherwise wait for STARTED.
    if hass.is_running:
        await _deferred_start()
    else:
        # async_listen_once auto-removes the listener when the event fires.
        # Calling unsub() after that point makes HA log a noisy "Unable to
        # remove unknown job listener" warning with a traceback (HA catches
        # the ValueError internally, so we can't swallow it from our side).
        # Track the fire ourselves and skip the unsub call entirely if the
        # listener has already auto-removed itself.
        listener_fired = {"v": False}

        async def _on_started(event) -> None:
            listener_fired["v"] = True
            await _deferred_start(event)

        unsub = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)

        @callback
        def _safe_unsub() -> None:
            if listener_fired["v"]:
                return  # already auto-removed by HA — nothing to do
            try:
                unsub()
            except (ValueError, KeyError):
                pass

        entry.async_on_unload(_safe_unsub)

    # Critical: return True right away so HA can finish booting
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a single config entry cleanly."""
    # Stop the hub first (so WS closes immediately)
    hub: SignalRClient | None = hass.data[DOMAIN].get(entry.entry_id, {}).get("hub")
    if hub:
        await hub.async_stop()
        _LOGGER.debug("[%s] Hub stopped", entry.entry_id)

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        _LOGGER.debug("[%s] Entry data cleared", entry.entry_id)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Called when the integration is being deleted by the user.

    Cleans up Hartmann-side state created by the managed-schedules feature:
    repoints every Active door back to its original DoorTimeZoneId and
    deletes every HA-tagged DoorTimeZone. Best-effort — failures here only
    leave harmless orphans in Hartmann that the user can clean up manually.
    """
    # We need the runtime cfg (base_url, partition_id, etc.) to make API
    # calls, which async_unload_entry just popped. Rebuild a minimal one
    # transient cfg for the cleanup pass.
    hass.data.setdefault(DOMAIN, {})
    if entry.entry_id not in hass.data[DOMAIN]:
        hass.data[DOMAIN][entry.entry_id] = {
            "base_url":       entry.data["base_url"],
            "username":       entry.data["username"],
            "password":       entry.data["password"],
            "session_cookie": entry.data["session_cookie"],
            "partition_id":   entry.data["partition_id"],
        }
        transient = True
    else:
        transient = False

    try:
        from . import managed_schedules
        await managed_schedules.cleanup_all(hass, entry)
    except Exception as e:
        _LOGGER.warning("[%s] cleanup_all on remove failed: %s", entry.entry_id, e)
    finally:
        if transient:
            hass.data[DOMAIN].pop(entry.entry_id, None)


def _options_diff_is_runtime_only(old: dict, new: dict) -> bool:
    """Return True if the only differences between old and new options are
    runtime tracking fields that don't require a platform reload.

    Specifically, when set_door_schedule_mode updates the current_mode of
    HA-managed doors, it writes to entry.options to persist the value across
    restarts. That options write triggers our update listener, which would
    otherwise reload the entire entry — destroying and recreating every
    entity and briefly making them all 'unavailable'. None of the platforms
    actually care about the current_mode value, so we suppress the reload.

    Returns True ONLY if every difference is inside managed_doors and
    limited to current_mode keys. Any other change (door added/removed from
    managed set, override_minutes changed, plan_ids changed, etc.) returns
    False so the reload still happens.
    """
    if set(old.keys()) != set(new.keys()):
        return False  # a top-level key was added/removed
    for k in old:
        if old[k] == new[k]:
            continue
        if k != "managed_doors":
            return False  # something other than managed_doors changed
        # managed_doors changed: is the change limited to current_mode?
        old_md = old[k] or {}
        new_md = new[k] or {}
        if set(old_md.keys()) != set(new_md.keys()):
            return False  # door added/removed
        for door_id, old_entry in old_md.items():
            new_entry = new_md.get(door_id) or {}
            if old_entry == new_entry:
                continue
            old_keys = set((old_entry or {}).keys())
            new_keys = set(new_entry.keys())
            if old_keys != new_keys:
                return False
            # Compare every key except current_mode
            for fk in old_keys:
                if fk == "current_mode":
                    continue
                if (old_entry or {}).get(fk) != new_entry.get(fk):
                    return False
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload on real options changes; skip reloads on:
       - pure title updates (e.g. partition rename in Hartmann)
       - runtime-only options writes (e.g. managed_doors current_mode tracking
         after a set_door_schedule_mode call)

    Both of these used to trigger a full entry reload, which destroys and
    recreates every entity — making them all briefly 'unavailable' in the
    Activity log even though nothing about the entities themselves changed.
    """
    cfg = hass.data[DOMAIN].get(entry.entry_id, {})
    last_options = cfg.get("_last_options_seen") or {}
    new_options = dict(entry.options)

    if last_options == new_options:
        _LOGGER.debug(
            "[%s] Entry update without options change (likely title rename) — skipping reload",
            entry.entry_id,
        )
        return

    if _options_diff_is_runtime_only(last_options, new_options):
        _LOGGER.debug(
            "[%s] Options change is runtime-only (managed_doors current_mode); skipping reload",
            entry.entry_id,
        )
        cfg["_last_options_seen"] = new_options
        return

    cfg["_last_options_seen"] = new_options
    _LOGGER.debug("[%s] Options updated; reloading entry", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)


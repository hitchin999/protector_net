from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import ssl
import random
from typing import Any, Dict, Optional, Tuple, List, Set

from aiohttp import ClientError, ClientSession, TCPConnector, WSMsgType
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers.dispatcher import async_dispatcher_send

try:
    from .const import (
        DOMAIN,
        FRIENDLY_TO_TZ_INDEX,  # map REST TimeZone strings → index
        KEY_DOOR_CONTACT_MAP,
        KEY_INPUT_STATE_CACHE,
        KEY_DOOR_CONTACT_STATE_CACHE,
        KEY_LAST_DOOR_STATUS,
        KEY_DOOR_HELD_OPEN_THRESHOLDS,
        DEFAULT_HELD_OPEN_THRESHOLD_MS,
    )
except Exception:
    from .const import DOMAIN
    FRIENDLY_TO_TZ_INDEX = {}
    KEY_DOOR_CONTACT_MAP = "door_contact_map"
    KEY_INPUT_STATE_CACHE = "input_state_cache"
    KEY_DOOR_CONTACT_STATE_CACHE = "door_contact_state_cache"
    KEY_LAST_DOOR_STATUS = "last_door_status"
    KEY_DOOR_HELD_OPEN_THRESHOLDS = "door_held_open_thresholds"
    DEFAULT_HELD_OPEN_THRESHOLD_MS = 30000

_LOGGER = logging.getLogger(f"{DOMAIN}.ws")

SIGNALR_RS = "\x1e"  # record separator
DISPATCH_DOOR = f"{DOMAIN}_door_event"        # sensors/switch/select listen on f"{...}_{entry_id}"
DISPATCH_HUB  = f"{DOMAIN}_hub_event"
DISPATCH_LOG  = f"{DOMAIN}_door_log"          # Last Door Log sensor
DISPATCH_DOOR_CONTACT = f"{DOMAIN}_door_contact"   # Door open/closed binary_sensor


def _is_transient_outage(exc: BaseException) -> bool:
    """Return True if the exception looks like a server-down/restart event.

    These happen routinely (e.g. nightly reboots of the Hartmann box) and
    aren't actionable for the user — we don't want them surfacing as WARNING
    in HA's notifications. Real protocol errors (parse failure, auth, etc.)
    still get logged at WARNING.
    """
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError)):
        return True
    # aiohttp's ClientError covers ClientConnectorError, ServerDisconnectedError,
    # ClientOSError, ServerTimeoutError, etc. — all reboot-class signals.
    if isinstance(exc, ClientError):
        return True
    # Defensive name-based match — covers httpx exceptions that bubble up
    # from api.py calls and any wrapper exceptions across library versions.
    cls_name = type(exc).__name__.lower()
    if any(tok in cls_name for tok in ("timeout", "connect", "disconnect",
                                       "unreachable", "remoteprotocol",
                                       "networkerror", "transporterror")):
        return True
    msg = str(exc).lower()
    if "connection timeout" in msg or "cannot connect to host" in msg:
        return True
    return False


def _mk_ssl_context(verify: bool) -> Optional[ssl.SSLContext]:
    """Return SSL context (None = default verify)."""
    if verify:
        return None  # default verify via aiohttp
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class SignalRClient:
    """Minimal SignalR JSON receiver for Protector.Net /rt/notificationHub."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

        # ❗ keep track of which partition this HA entry belongs to
        self._partition_id: int | None = None

        # Partition-scoped allowlist and maps
        self._allowed_door_ids: Set[int] = set()
        self._door_map: Dict[str, Tuple[int, str]] = {}
        self._name_index: Dict[str, int] = {}
        self._reader_by_id: dict[int, int] = {}
        self._reader_by_name: dict[str, int] = {}
        self._last_door_status_at: dict[int, float] = {}
        self._baseline_reader_tz: dict[int, int] = {}

        # Actively managed handles
        self._session: ClientSession | None = None
        self._ws: Any | None = None

        # Hub status attrs
        self.phase = "idle"
        self.connected = False
        self.last_error: str | None = None
        self.last_event_ts: float | None = None
        self.last_connect_ts: float | None = None

        # connection info
        self.ws_url: str | None = None
        self.connection_token: str | None = None
        self._subscribed_panels: List[str] = []

        # counters / last seen
        self.door_events_seen = 0
        self.non_door_events_seen = 0
        self.last_statusType: str | None = None
        self.last_statusId: str | None = None
        self.last_door_payload: Dict[str, Any] | None = None
        self.last_log_line: str | None = None

        # --- Odyssey capability detection + sync task handle ---
        self._supports_status_snapshot: Optional[bool] = None
        self._sync_task: Optional[asyncio.Task] = None
        self._unsub_stop: Optional[Any] = None

        # --- Held-open synthesis (Protector.Net workaround) ---------------
        # Per-door asyncio.Task that fires AllowedHeldOpenTime ms after a
        # door opens, dispatching a synthesized HELD_OPEN event to the
        # binary_sensor. Necessary because Protector.Net (legacy) doesn't
        # emit a DOOR_CONTACT_STATE | HELD_OPEN notification — its web UI
        # derives the "Held Open" badge purely client-side from contact
        # on-time. Odyssey panels DO emit a real HELD_OPEN notification,
        # so on those servers our synthesized dispatch is just defensive
        # backup (the real notif arrives first and the timer is moot).
        #
        # Keyed by Hartmann door_id. Cancelled on close, on stop, and on
        # WS reconnect (a stale task must not survive a bouncing connection).
        self._held_open_timers: Dict[int, asyncio.Task] = {}

    # Public -----------------------------------------------------------------

    def async_start(self) -> None:
        """Start the background websocket task."""
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = self.hass.async_create_task(self._runner())

        # Ensure clean shutdown: stop WS when HA is stopping so tasks
        # don't survive past the "final writes" stage.
        if self._unsub_stop is None:
            async def _on_ha_stop(_event) -> None:
                await self.async_stop()
            self._unsub_stop = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, _on_ha_stop
            )

    async def async_stop(self) -> None:
        """Stop the WS client quickly and deterministically so HA can reload."""
        self._stop.set()

        # Unsubscribe HA stop listener (avoid double-fire on reload vs shutdown)
        if self._unsub_stop:
            self._unsub_stop()
            self._unsub_stop = None

        # Cancel periodic sync loop if running
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sync_task
        self._sync_task = None

        # Cancel any in-flight held-open timers. If we don't, an HA reload
        # that lands inside the held-open window would fire the dispatch
        # against the new entry's entities, double-counting state.
        self._cancel_all_held_open_timers()

        # Proactively close the websocket/session so the recv loop breaks now
        try:
            if self._ws and not getattr(self._ws, "closed", True):
                await self._ws.close(code=1000, message=b"homeassistant-reload")
        except Exception:
            pass

        try:
            if self._session and not self._session.closed:
                await self._session.close()
        except Exception:
            pass

        # Ensure background task exits
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                try:
                    await asyncio.wait_for(self._task, timeout=3)
                except asyncio.TimeoutError:
                    _LOGGER.debug("[%s] WS task did not exit in time; continuing", self.entry_id)

        self._task = None
        self._ws = None
        self._session = None

    # Hub sensor helpers ------------------------------------------------------

    @callback
    def _push_hub_state(self) -> None:
        data = {
            "phase": self.phase,
            "connected": self.connected,
            "last_error": self.last_error or "None",
            "last_event_ts": self.last_event_ts,
            "last_connect_ts": self.last_connect_ts,
            "mapped_doors": len(self._door_map),
            "ws_url": self.ws_url,
            "connection_token": self.connection_token,
            "door_events_seen": self.door_events_seen,
            "non_door_events_seen": self.non_door_events_seen,
            "last_statusType": self.last_statusType,
            "last_statusId": self.last_statusId,
            "last_door_payload": self.last_door_payload,
            "last_log_line": self.last_log_line,
            "partition_id": self._partition_id,
            "supports_status_snapshot": self._supports_status_snapshot,
        }

        async_dispatcher_send(self.hass, f"{DISPATCH_HUB}_{self.entry_id}", data)

    # Internals ---------------------------------------------------------------

    def _normalize_name(self, s: str) -> str:
        return re.sub(r"\s+", " ", (s or "").strip().lower())

    def _strip_reader_suffix(self, s: str) -> str:
        """Remove trailing 'reader', 'reader <num>', 'door', or 'gate'."""
        s = self._normalize_name(s)
        s = re.sub(r"\s+reader(\s+\d+)?$", "", s)  # 'reader' or 'reader 2'
        s = re.sub(r"\s+(door|gate)$", "", s)      # 'door' or 'gate'
        return s.strip()

    def _recent_real_status(self, door_id: int, window: float = 1.0) -> bool:
        """True if a real door status frame arrived within <window> seconds."""
        ts = self._last_door_status_at.get(door_id)
        if ts is None:
            return False
        return (self.hass.loop.time() - ts) <= window

    async def _refresh_allowed_doors(self) -> None:
        """Populate the set of allowed door IDs for THIS entry’s partition."""
        from . import api
        try:
            doors = await api.get_all_doors(self.hass, self.entry_id)
            self._allowed_door_ids = {int(d["Id"]) for d in doors or [] if "Id" in d}
        except Exception as e:
            _LOGGER.warning("[%s] Failed to fetch allowed doors (will retry): %s", self.entry_id, e)
            self._allowed_door_ids = set()

    async def _build_door_map(self) -> None:
        """Build filtered maps limited to this entry's partition doors."""
        from . import api

        # Ensure allowlist first
        if not self._allowed_door_ids:
            await self._refresh_allowed_doors()

        try:
            ov = await api.get_system_overview(self.hass, self.entry_id)
        except Exception as e:
            if _is_transient_outage(e):
                _LOGGER.info(
                    "[%s] System overview fetch failed (server unreachable, will retry): %s",
                    self.entry_id, e,
                )
            else:
                _LOGGER.warning(
                    "[%s] Failed to fetch system overview (will retry) [%s]: %s",
                    self.entry_id, type(e).__name__, e,
                )
            self._door_map = {}
            self._reader_by_id = {}
            self._reader_by_name = {}
            self._name_index = {}
            return

        door_map: Dict[str, Tuple[int, str]] = {}
        reader_by_id: dict[int, int] = {}
        reader_by_name: dict[str, int] = {}
        name_index: dict[str, int] = {}

        def walk(node: Dict[str, Any], current_door: Optional[Tuple[int, str]] = None) -> None:
            for sub in node.get("Nodes", []) or []:
                ntype = sub.get("Type")
                if ntype == "Door":
                    sid = sub.get("StatusId")
                    did = sub.get("Id")
                    name = sub.get("Name")
                    if isinstance(did, int) and did in self._allowed_door_ids:
                        if sid:
                            door_map[str(sid)] = (int(did), str(name))
                        if name:
                            name_index[self._normalize_name(str(name))] = int(did)
                        walk(sub, (int(did), str(name)))
                    else:
                        # Skip doors not in our partition
                        walk(sub, None)

                elif ntype == "Reader" and current_door:
                    rid = sub.get("Id")
                    rname_raw = (sub.get("Name") or "").strip()
                    if isinstance(rid, int):
                        reader_by_id[int(rid)] = current_door[0]
                    if rname_raw:
                        reader_by_name[rname_raw.lower()] = current_door[0]
                        base = self._strip_reader_suffix(rname_raw)
                        if base and base != rname_raw.lower():
                            reader_by_name[base] = current_door[0]
                    walk(sub, current_door)
                else:
                    walk(sub, current_door)

        root = (ov or {}).get("Status", {})
        for site in root.get("Nodes", []) or []:
            walk(site, None)

        self._door_map = door_map
        self._reader_by_id = reader_by_id
        self._reader_by_name = reader_by_name
        self._name_index = name_index

        # ---- extra: pull explicit partition readers and merge ----
        try:
            from . import api as _api
            extra_readers = await _api.get_available_readers(self.hass, self.entry_id)
        except Exception as e:
            extra_readers = []
            _LOGGER.warning("[%s] Failed to fetch partition readers: %s", self.entry_id, e)

        merged = 0
        if extra_readers:
            for rd in extra_readers:
                rid = rd.get("Id")
                door_id_from_api = rd.get("DoorId")
                rname = (rd.get("Name") or "").strip()

                if not isinstance(rid, int) or not isinstance(door_id_from_api, int):
                    continue

                # ❗ stay inside the doors we got from get_all_doors()
                if self._allowed_door_ids and door_id_from_api not in self._allowed_door_ids:
                    # reader is for some OTHER partition -> skip
                    continue

                # 1) reader-id -> door-id
                self._reader_by_id[rid] = door_id_from_api

                # 2) reader-name -> door-id (plus "Kitchen Reader" → "Kitchen")
                if rname:
                    norm = rname.lower()
                    self._reader_by_name[norm] = door_id_from_api
                    base = self._strip_reader_suffix(rname)
                    if base and base != norm:
                        self._reader_by_name[base] = door_id_from_api

                merged += 1

        _LOGGER.debug(
            "[%s] Built maps (filtered): doors=%d, readers_by_id=%d (+%d from partition), readers_by_name=%d, names=%d",
            self.entry_id,
            len(self._door_map),
            len(self._reader_by_id),
            merged,
            len(self._reader_by_name),
            len(self._name_index),
        )

        if self._door_map:
            sample = {k: v for k, v in list(self._door_map.items())[:10]}
            _LOGGER.debug("[%s] Map sample: %s", self.entry_id, sample)

    def _panels_from_map(self) -> List[str]:
        ctrls: set[str] = set()
        for sid in self._door_map.keys():
            root = str(sid).split("::", 1)[0]
            if root:
                ctrls.add(root)
        return sorted(ctrls)

    async def _negotiate(self, session: ClientSession, base: str, cookie: str) -> tuple[str, str]:
        """Negotiate a SignalR connection token; re-auth on 401.

        Returns (connection_token, cookie_header) — cookie may be refreshed.
        """
        from . import api

        url = f"{base}/rt/notificationHub/negotiate?negotiateVersion=1"
        headers = {"Cookie": cookie, "Content-Type": "text/plain"}
        async with session.post(url, headers=headers, data=b"") as resp:
            if resp.status == 401:
                _LOGGER.debug("[%s] Negotiate got 401; re-authenticating", self.entry_id)
                cfg = self.hass.data[DOMAIN][self.entry_id]
                new_cookie = await api.login(
                    self.hass, cfg["base_url"], cfg["username"], cfg["password"],
                )
                cfg["session_cookie"] = new_cookie
                cookie = f"ss-id={new_cookie}"
                async with session.post(url, headers={"Cookie": cookie, "Content-Type": "text/plain"}, data=b"") as resp2:
                    resp2.raise_for_status()
                    data = await resp2.json()
                    return data["connectionToken"], cookie
            resp.raise_for_status()
            data = await resp.json()
            return data["connectionToken"], cookie

    async def _send_invocation(self, ws, target: str, args: list[Any], inv_id: str) -> None:
        frame = {
            "type": 1,
            "target": target,
            "arguments": args,
            "invocationId": inv_id,
            "streamIds": [],
        }
        await ws.send_str(json.dumps(frame) + SIGNALR_RS)

    # ---------- Held-open synthesis (Protector.Net workaround) ----------

    def _resolve_held_open_threshold_ms(self, door_id: int) -> int:
        """Look up the door's held-open threshold in milliseconds.

        Reads the cache populated by `api.build_door_held_open_thresholds`
        on load and on the hourly sync. Falls back to
        DEFAULT_HELD_OPEN_THRESHOLD_MS when the cache hasn't been built
        yet (first WS connect can race the post-setup sync) or when the
        door isn't in the cache (added in Hartmann mid-session, before the
        next hourly tick rebuilds the map). Default is 30s — matches
        Hartmann's factory AllowedHeldOpenTime.
        """
        cache = (self.hass.data.get(DOMAIN, {})
                 .get(self.entry_id, {})
                 .get(KEY_DOOR_HELD_OPEN_THRESHOLDS) or {})
        val = cache.get(door_id)
        if isinstance(val, int) and val > 0:
            return val
        return DEFAULT_HELD_OPEN_THRESHOLD_MS

    def _cancel_held_open_timer(self, door_id: int) -> None:
        """Cancel any pending held-open timer for this door.

        Called on every door-close transition and on WS shutdown/reconnect.
        Cancellation is idempotent: a finished or never-started timer is a
        silent no-op.
        """
        task = self._held_open_timers.pop(door_id, None)
        if task and not task.done():
            task.cancel()

    def _cancel_all_held_open_timers(self) -> None:
        """Cancel every pending held-open timer.

        Called on hub stop and on WS reconnect. The reconnect cancellation
        is critical: the Hartmann panel will resend a fresh DOOR_CONTACT
        baseline after reconnect, so any stale timer from before the drop
        would race against the re-baseline and could fire even if the door
        already closed during the outage.
        """
        for task in list(self._held_open_timers.values()):
            if task and not task.done():
                task.cancel()
        self._held_open_timers.clear()

    def _start_held_open_timer(self, door_id: int, ts: Optional[str]) -> None:
        """Schedule a held-open dispatch for `door_id`.

        Cancels any existing timer for this door first (so a rapid
        open→close→open cycle resets the clock, matching Hartmann's UI
        behavior). The dispatched payload uses `synthesized=True` so
        consumers can distinguish a derived held-open from a real Odyssey
        HELD_OPEN notification — useful for diagnostics.
        """
        self._cancel_held_open_timer(door_id)

        threshold_ms = self._resolve_held_open_threshold_ms(door_id)
        if threshold_ms <= 0:
            return

        _LOGGER.debug(
            "[%s] Held-open timer armed for door_id=%s (fires in %dms)",
            self.entry_id, door_id, threshold_ms,
        )

        async def _runner() -> None:
            try:
                await asyncio.sleep(threshold_ms / 1000.0)
            except asyncio.CancelledError:
                return

            # Defensive: if the contact-state cache says the door is no
            # longer open (a CLOSED notif arrived but cancellation racing
            # past us), don't fire. Prevents a stale held-open spike.
            cfg_bucket = (self.hass.data.get(DOMAIN, {})
                          .get(self.entry_id, {}))
            ds_cache = cfg_bucket.get(KEY_DOOR_CONTACT_STATE_CACHE) or {}
            current = ds_cache.get(door_id)
            if isinstance(current, dict) and not current.get("is_open", True):
                _LOGGER.debug(
                    "[%s] Held-open timer fired for door_id=%s but cache "
                    "shows closed; suppressing synth",
                    self.entry_id, door_id,
                )
                return

            _LOGGER.debug(
                "[%s] Synthesizing HELD_OPEN for door_id=%s (threshold=%dms)",
                self.entry_id, door_id, threshold_ms,
            )

            # Update the cache so a freshly-restored entity that comes up
            # mid-held-open seeds itself correctly without waiting for the
            # next state change.
            if isinstance(ds_cache, dict):
                prev = ds_cache.get(door_id, {}) or {}
                ds_cache[door_id] = {
                    "is_open":      True,
                    "held_open":    True,
                    "ts":           ts or prev.get("ts"),
                    "state_values": "HELD_OPEN",
                }

            async_dispatcher_send(
                self.hass,
                f"{DISPATCH_DOOR_CONTACT}_{self.entry_id}",
                {
                    "source":       "notification",
                    "door_id":      door_id,
                    "is_open":      True,
                    "held_open":    True,
                    "state_values": "HELD_OPEN",
                    "ts":           ts,
                    "synthesized":  True,
                },
            )

        self._held_open_timers[door_id] = self.hass.async_create_task(_runner())

    # ---------- Notification/Name helpers ----------

    def _door_id_from_text(self, txt: str) -> Optional[int]:
        norm = self._normalize_name(txt)
        if not norm:
            return None

        variants = {norm}
        stripped = self._strip_reader_suffix(norm)
        if stripped:
            variants.add(stripped)

        for v in variants:
            did = self._name_index.get(v)
            if did:
                return did

        for v in variants:
            for name_norm, door_id in self._name_index.items():
                if name_norm in v or v in name_norm:
                    return door_id

        return None

    def _door_id_from_notification(self, note: Dict[str, Any]) -> Optional[int]:
        src_type = (note.get("SourceType") or "").strip()
        src_name = (note.get("SourceName") or "").strip()
        src_id = note.get("SourceId")
        msg = (note.get("Message") or "").strip()

        # If the source itself is a Door, SourceId is already the door id
        if src_type.lower() == "door" and isinstance(src_id, int):
            return int(src_id)

        # Reader sources
        if src_type.lower() == "reader":
            if isinstance(src_id, int) and src_id in self._reader_by_id:
                return self._reader_by_id[int(src_id)]
            if src_name:
                rid = self._reader_by_name.get(src_name.lower())
                if rid is not None:
                    return rid
                base = self._strip_reader_suffix(src_name)
                if base:
                    rid = self._reader_by_name.get(base)
                    if rid is not None:
                        return rid
                    did = self._door_id_from_text(base)
                    if did is not None:
                        return did

        # Try names in SourceName / Message
        for candidate in (src_name, msg):
            did = self._door_id_from_text(candidate or "")
            if did is not None:
                return did

        # Fallback phrasing "... to/on/for <Door Name>"
        m = re.search(r"\b(?:to|on|for)\s+(.+)$", msg, flags=re.IGNORECASE)
        if m:
            did = self._door_id_from_text(m.group(1))
            if did is not None:
                return did

        return None

    # ---------- Odyssey capability + REST snapshot helpers ----------

    async def _detect_odyssey_status_support(self) -> None:
        """Probe once: if /api/Doors/{id}/Status exists on this server, enable snapshots."""
        if self._supports_status_snapshot is not None:
            return
        # Ensure we have something to probe
        if not self._allowed_door_ids:
            await self._refresh_allowed_doors()
        test_did = next(iter(self._allowed_door_ids), None)
        if test_did is None:
            self._supports_status_snapshot = False
            _LOGGER.debug("[%s] No doors available to probe snapshot support", self.entry_id)
            return
        from . import api
        try:
            js = await api.get_door_status(self.hass, self.entry_id, int(test_did))
        except Exception as e:
            js = None
            _LOGGER.debug("[%s] Snapshot probe error for door %s: %s", self.entry_id, test_did, e)
        if js and isinstance(js, dict) and ("TimeZone" in js or "Lock" in js):
            self._supports_status_snapshot = True
            _LOGGER.debug("[%s] Odyssey door status endpoint detected; snapshots enabled", self.entry_id)
        else:
            self._supports_status_snapshot = False
            _LOGGER.debug("[%s] Odyssey door status endpoint not available; snapshots disabled", self.entry_id)

    def _map_rest_status_to_payload(self, js: dict) -> dict[str, Any]:
        """
        Convert Odyssey REST door status into the WS-like compact payload the rest
        of the integration expects: {strike, opener, overridden, timeZone}
        """
        # Lock: "Locked"/"Unlocked" (treat unknown as None)
        lock_s = (js.get("Lock") or "").strip().lower()
        if lock_s == "locked":
            strike = False
            opener = False
        elif lock_s == "unlocked":
            strike = True
            opener = True
        else:
            strike = None
            opener = None

        # Override: "NoOverride" vs any other value
        ov = js.get("Override")
        if ov is None:
            overridden: Optional[bool] = None
        else:
            overridden = False if str(ov).lower() == "nooverride" else True

        # TimeZone: token like "Card", "CardOrPin", "Unlock", etc. → numeric index
        tz_token = js.get("TimeZone")
        tz_idx: Optional[int] = None
        if tz_token is not None:
            if isinstance(tz_token, int):
                tz_idx = tz_token
            else:
                token = str(tz_token).strip()
                # Try FRIENDLY_TO_TZ_INDEX (expects human labels like "Card or Pin")
                # First, common normalizations:
                normalized_candidates = {
                    token,
                    token.replace(" ", ""),           # "Card Or Pin" → "CardOrPin"
                    token.replace("_", ""),           # "Card_Or_Pin" → "CardOrPin"
                    token.title().replace("Or", "or") # best-effort title case
                }
                # Direct known tokens
                token_map = {
                    "Lockdown": 0,
                    "Card": 1,
                    "Pin": 2,
                    "CardOrPin": 3,
                    "Card or Pin": 3,
                    "CardAndPin": 4,
                    "Card and Pin": 4,
                    "Unlock": 5,
                    "FirstCredentialIn": 6,
                    "First Credential In": 6,
                    "DualCredential": 7,
                    "Dual Credential": 7,
                }
                for cand in normalized_candidates:
                    if cand in token_map:
                        tz_idx = token_map[cand]
                        break
                if tz_idx is None:
                    # Try friendly map (case-insensitive)
                    for friendly, idx in (FRIENDLY_TO_TZ_INDEX or {}).items():
                        if friendly.lower() == token.lower():
                            tz_idx = idx
                            break

        return {"strike": strike, "opener": opener, "overridden": overridden, "timeZone": tz_idx}

    async def _sync_all_statuses(self, reason: str = "periodic") -> None:
        """Snapshot all allowed doors from REST and dispatch as statuses (Odyssey only)."""
        if not self._supports_status_snapshot:
            return
        # need an allowlist
        if not self._allowed_door_ids:
            await self._refresh_allowed_doors()
        if not self._allowed_door_ids:
            return

        from . import api
        for did in sorted(self._allowed_door_ids):
            try:
                js = await api.get_door_status(self.hass, self.entry_id, int(did))
            except Exception:
                js = None
            if not js or not isinstance(js, dict):
                continue
            payload = self._map_rest_status_to_payload(js)

            # Cache baseline reader mode when not overridden
            try:
                tz_val = payload.get("timeZone")
                ov_val = payload.get("overridden")
                if tz_val is not None and ov_val is False:
                    self._baseline_reader_tz[int(did)] = int(tz_val)
            except Exception:
                pass

            _LOGGER.debug("[%s] %s sync -> door_id=%s payload=%s", self.entry_id, reason, did, payload)
            async_dispatcher_send(
                self.hass,
                f"{DISPATCH_DOOR}_{self.entry_id}",
                {"door_id": int(did), "status": payload},
            )

    async def _periodic_sync_loop(self) -> None:
        """Run lightweight periodic syncs while connected (captures schedule flips)."""
        if not self._supports_status_snapshot:
            return
        try:
            await asyncio.sleep(5)  # let WS settle
            while self.connected and not self._stop.is_set():
                await self._sync_all_statuses("periodic")
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=60 + random.uniform(0, 3)
                    )
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            pass
        except Exception as e:
            _LOGGER.debug("[%s] periodic sync loop ended: %s", self.entry_id, e)

    # ---------- Runner / message loop ----------

    async def _runner(self) -> None:
        """Main websocket loop."""
        cfg = self.hass.data[DOMAIN][self.entry_id]
        base: str = cfg["base_url"]
        verify_ssl: bool = bool(cfg.get("verify_ssl", False))
        
        # 👇 capture partition from the entry config (what you selected in config_flow)
        self._partition_id = cfg.get("partition_id")

        self.phase = "starting"
        self._push_hub_state()

        ssl_ctx = _mk_ssl_context(verify_ssl)
        connector = TCPConnector(ssl=ssl_ctx)

        base_backoff = 5.0
        max_backoff  = 30.0
        backoff      = base_backoff

        self._session = None
        self._ws = None

        try:
            async with ClientSession(connector=connector, trust_env=False) as session:
                self._session = session
                while not self._stop.is_set():
                    # Read cookie fresh each iteration — _request_with_reauth
                    # (called by _build_door_map) may have refreshed it after a 401.
                    cookie = f"ss-id={cfg['session_cookie']}"
                    try:
                        await self._build_door_map()
                        # Re-read cookie after _build_door_map in case it triggered re-auth
                        cookie = f"ss-id={cfg['session_cookie']}"
                        await self._detect_odyssey_status_support()

                        token, cookie = await self._negotiate(session, base, cookie)
                        self.connection_token = token
                        scheme = "wss" if base.startswith("https") else "ws"
                        self.ws_url = f"{scheme}://{base.split('://',1)[1]}/rt/notificationHub?id={token}"

                        self.phase = "connecting"
                        self._push_hub_state()
                        async with session.ws_connect(
                            self.ws_url,
                            headers={"Cookie": cookie, "X-Requested-With": "XMLHttpRequest"},
                            heartbeat=30,
                        ) as ws:
                            self._ws = ws
                            backoff = base_backoff

                            self.connected = True
                            self.last_connect_ts = self.hass.loop.time()
                            self.phase = "handshake"
                            self._push_hub_state()

                            # SignalR handshake
                            await ws.send_str(json.dumps({"protocol": "json", "version": 1}) + SIGNALR_RS)

                            # Subscribe to panels
                            ctrls = self._panels_from_map()
                            self._subscribed_panels = ctrls[:]
                            try:
                                await self._send_invocation(ws, "Init", [None, None], "1")
                                if ctrls:
                                    await self._send_invocation(ws, "subscribeToStatus", [ctrls], "2")
                                    _LOGGER.debug("[%s] Subscribed to panels: %s", self.entry_id, ctrls)
                                else:
                                    _LOGGER.debug("[%s] No panels found to subscribe.", self.entry_id)
                            except Exception as e:
                                _LOGGER.debug("[%s] subscribe/init invocation failed: %s", self.entry_id, e)

                            self.phase = "running"
                            self._push_hub_state()
                            _LOGGER.debug("[%s] WS connected. Listening… (mapped_doors=%d)",
                                          self.entry_id, len(self._door_map))

                            # ---- Odyssey: immediate snapshot + periodic sync (gated) ----
                            if self._supports_status_snapshot:
                                self.hass.async_create_task(self._sync_all_statuses("connect"))
                                if self._sync_task and not self._sync_task.done():
                                    self._sync_task.cancel()
                                self._sync_task = self.hass.async_create_task(self._periodic_sync_loop())

                            async for msg in ws:
                                if self._stop.is_set():
                                    break

                                if msg.type == WSMsgType.TEXT:
                                    await self._handle_text(msg.data)
                                elif msg.type == WSMsgType.BINARY:
                                    self.non_door_events_seen += 1
                                    self._push_hub_state()
                                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                                    raise ClientError(f"WS closed: {msg.type}")

                    except asyncio.CancelledError:
                        break

                    except Exception as e:
                        # error + schedule retry with backoff + jitter
                        self.connected = False
                        self.phase = "error"
                        self.last_error = str(e)

                        # Cancel any in-flight held-open timers — they were
                        # scheduled against the old WS session's view of
                        # state. After reconnect Hartmann sends a fresh
                        # baseline (current door states), so we'll restart
                        # any needed timers from those frames. Letting the
                        # old timers run risks firing held-open against a
                        # door that closed during the outage but whose
                        # CLOSED notification was lost in the drop.
                        self._cancel_all_held_open_timers()
                        # Server reboots / unreachable network are routine and
                        # not actionable. Log them at INFO so they don't
                        # clutter the WARNING surface; real errors stay at
                        # WARNING.
                        if _is_transient_outage(e):
                            _LOGGER.info(
                                "[%s] WS connection lost (server unreachable, will retry): %s",
                                self.entry_id, e,
                            )
                        else:
                            _LOGGER.warning(
                                "[%s] WS error (will retry) [%s]: %s",
                                self.entry_id, type(e).__name__, e,
                            )
                        self._push_hub_state()

                        # stop periodic sync if running
                        if self._sync_task and not self._sync_task.done():
                            self._sync_task.cancel()
                        self._sync_task = None

                        sleep_for = min(backoff, max_backoff) + random.uniform(0, 1.5)
                        _LOGGER.debug("[%s] Reconnecting in %.2fs (backoff=%.1fs, cap=%.1fs)",
                                      self.entry_id, sleep_for, backoff, max_backoff)
                        try:
                            await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
                            break
                        except asyncio.TimeoutError:
                            pass
                        backoff = min(backoff * 2.0, max_backoff)

                    finally:
                        self._ws = None
        finally:
            self._session = None
            self.connected = False
            self.phase = "stopped"
            # stop periodic sync if running
            if self._sync_task and not self._sync_task.done():
                self._sync_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._sync_task
            self._sync_task = None
            self._push_hub_state()

    async def _handle_text(self, payload: str) -> None:
        """Process inbound frames."""
        frames = [f for f in payload.split(SIGNALR_RS) if f]
        for frame in frames:
            try:
                data = json.loads(frame)
            except Exception:
                self.last_log_line = f"Bad JSON frame (len={len(frame)})"
                _LOGGER.debug("[%s] Bad JSON frame: %s", self.entry_id, frame[:200])
                continue

            # ---- Door status frames ----
            if data.get("type") == 1 and data.get("target") == "status":
                args = data.get("arguments") or []
                if not args:
                    continue
                st = args[0]
                stype = st.get("statusType")
                sid = st.get("statusId")

                self.last_statusType = stype
                self.last_statusId = sid
                self.last_event_ts = self.hass.loop.time()

                if stype == "Door":
                    self.door_events_seen += 1

                    # --- normalize 'overridden' and 'timeZone' from WS (Odyssey may send 0/1 or strings) ---
                    ov_val = st.get("overridden", None)
                    if isinstance(ov_val, int):
                        st["overridden"] = bool(ov_val)
                    tz_val = st.get("timeZone", None)
                    if isinstance(tz_val, str):
                        try:
                            st["timeZone"] = int(tz_val)
                        except ValueError:
                            pass

                    compact = {k: st.get(k) for k in ("strike", "opener", "overridden", "timeZone")}
                    self.last_door_payload = compact

                    door = self._door_map.get(str(sid))
                    if not door:
                        # Likely a door on the same panel but outside our partition; keep quiet.
                        root = str(sid).split("::", 1)[0] if sid else ""
                        if root and (root in self._subscribed_panels):
                            _LOGGER.debug(
                                "[%s] Door frame for other door on subscribed panel (ignored): statusId=%s payload=%s",
                                self.entry_id, sid, compact
                            )
                            continue
                        # Otherwise, map might actually be stale -> refresh once
                        await self._build_door_map()
                        continue

                    door_id, door_name = door
                    # Double guard (should not happen due to filtered map)
                    if self._allowed_door_ids and door_id not in self._allowed_door_ids:
                        _LOGGER.debug(
                            "[%s] Door frame for other partition (ignored): door_id=%s payload=%s",
                            self.entry_id, door_id, compact
                        )
                        continue

                    # remember: real status just arrived for this door
                    self._last_door_status_at[door_id] = self.hass.loop.time()

                    # Cache baseline reader mode when not overridden
                    try:
                        tz_val2 = st.get("timeZone", None)
                        ov_val2 = st.get("overridden", None)
                        if tz_val2 is not None and not ov_val2:
                            try:
                                tz_int = int(tz_val2)
                            except (TypeError, ValueError):
                                tz_int = None
                            if tz_int is not None:
                                self._baseline_reader_tz[door_id] = tz_int
                                _LOGGER.debug(
                                    "[%s] Baseline reader mode cached -> door_id=%s tz=%s",
                                    self.entry_id, door_id, tz_int
                                )
                    except Exception:
                        pass

                    self.last_log_line = f"Door frame -> door_id={door_id} ({door_name}) payload={compact}"
                    _LOGGER.debug("[%s] Door frame -> door_id=%s name=%s payload=%s",
                                  self.entry_id, door_id, door_name, compact)

                    # Cache last-seen status, merging non-None fields with
                    # any prior cached values. This way a partial frame
                    # (e.g., {strike: None, opener: None, overridden: False,
                    # timeZone: 1}) doesn't wipe a previously-cached
                    # strike/opener — exactly the pattern Hartmann uses
                    # when sending a multi-frame burst per door at startup.
                    # The post-setup re-dispatch in __init__.py reads from
                    # this cache to seed entities that may have subscribed
                    # after the WS burst already arrived.
                    try:
                        cache = (self.hass.data.get(DOMAIN, {})
                                 .get(self.entry_id, {})
                                 .get(KEY_LAST_DOOR_STATUS))
                        if isinstance(cache, dict):
                            prev = cache.get(door_id, {})
                            merged = dict(prev)
                            for k in ("strike", "opener", "overridden", "timeZone"):
                                v = st.get(k)
                                if v is not None:
                                    merged[k] = v
                            if merged != prev:
                                cache[door_id] = merged
                    except Exception:
                        pass

                    async_dispatcher_send(
                        self.hass,
                        f"{DISPATCH_DOOR}_{self.entry_id}",
                        {"door_id": door_id, "status": st},
                    )
                    self._push_hub_state()
                elif stype == "Input":
                    # Door-contact inputs come through as a separate status
                    # frame (not embedded in Door frames). statusId format is
                    # "<MAC>::Input::<idx>" and `enabled` is 0 or 1.
                    #
                    # Look up (mac, idx) in the contact map built at startup
                    # and on the hourly sync. If found, route to the
                    # binary_sensor; otherwise drop silently (most inputs are
                    # not door contacts — REX, motion, aux, disabled, etc.).
                    self.non_door_events_seen += 1
                    self._push_hub_state()

                    sid_str = str(sid or "")
                    parts = sid_str.split("::", 2)
                    if len(parts) != 3 or parts[1] != "Input":
                        continue
                    mac = parts[0]
                    try:
                        input_idx = int(parts[2])
                    except (TypeError, ValueError):
                        continue

                    enabled_raw = st.get("enabled")
                    # Normalize: WS sends 0/1, also tolerate booleans/strings.
                    if isinstance(enabled_raw, bool):
                        enabled = enabled_raw
                    elif isinstance(enabled_raw, (int, float)):
                        enabled = bool(int(enabled_raw))
                    elif isinstance(enabled_raw, str):
                        enabled = enabled_raw.strip().lower() in ("1", "true", "on", "yes")
                    else:
                        continue

                    # Cache the raw value in case the binary_sensor entity is
                    # added after this frame (e.g., entity restart, options
                    # reload). It'll read this on async_added_to_hass.
                    cfg_bucket = self.hass.data.get(DOMAIN, {}).get(self.entry_id, {})
                    cache = cfg_bucket.get(KEY_INPUT_STATE_CACHE)
                    if isinstance(cache, dict):
                        cache[(mac, input_idx)] = enabled

                    # Route to binary_sensor only if this input is a known
                    # door contact for this entry. Other inputs (REX, motion,
                    # etc.) are intentionally ignored — exposing them would
                    # widen the integration's surface area without a use case.
                    contact_map = cfg_bucket.get(KEY_DOOR_CONTACT_MAP) or {}
                    info = contact_map.get((mac, input_idx))
                    if not info:
                        continue

                    async_dispatcher_send(
                        self.hass,
                        f"{DISPATCH_DOOR_CONTACT}_{self.entry_id}",
                        {
                            "door_id":    info["door_id"],
                            "panel_mac":  mac,
                            "input_idx":  input_idx,
                            "enabled":    enabled,
                            "is_inverted": bool(info.get("is_inverted")),
                        },
                    )
                else:
                    self.non_door_events_seen += 1
                    self._push_hub_state()
                continue  # handled

            # ---- Notifications (for Last Door Log and override synth) ----
            if data.get("type") == 1 and data.get("target") == "notification":
                args = data.get("arguments") or []
                if not args:
                    continue
                notes_iter: List[Dict[str, Any]] = []
                first = args[0]
                if isinstance(first, list):
                    notes_iter = [n for n in first if isinstance(n, dict)]
                elif isinstance(first, dict):
                    notes_iter = [first]
                else:
                    for a in args:
                        if isinstance(a, dict):
                            notes_iter.append(a)

                allowed_door_ids = self._allowed_door_ids
                if not allowed_door_ids:
                    _LOGGER.warning(
                        "[%s] Notification arrived but allowed_door_ids is empty – dropping to avoid cross-partition mixups",
                        self.entry_id,
                    )
                    continue

                for note in notes_iter:
                    msg = note.get("Message") or ""
                    ntype = (note.get("NotificationType") or "").upper()

                    # 👇 hard partition gate from the WS client's configured partition
                    note_part = note.get("PartitionId")
                    if self._partition_id is not None and note_part is not None:
                        try:
                            if int(note_part) != int(self._partition_id):
                                # not for this partition -> ignore completely
                                continue
                        except (TypeError, ValueError):
                            # weird PartitionId -> fall through to door-id check
                            pass

                    did = self._door_id_from_notification(note)

                    if did is None:
                        if ntype.startswith("ACTIONPLAN_"):
                            continue
                        _LOGGER.debug("[%s] Unmapped notification: %s", self.entry_id, note)
                        self._push_hub_state()
                        continue

                    # ✅ final guard: door must be in THIS entry's allowlist
                    if did not in allowed_door_ids:
                        continue

                    # Route to "Last Door Log" sensor
                    async_dispatcher_send(
                        self.hass,
                        f"{DISPATCH_LOG}_{self.entry_id}",
                        {
                            "door_id": did,
                            "log": msg,
                            "raw": note,
                            "user_id": note.get("UserId"),
                            "notification_type": ntype,
                            "state": note.get("StateValues"),
                            "timestamp": note.get("Date"),
                            "partition_id": note.get("PartitionId"),
                            "source": {
                                "type": note.get("SourceType"),
                                "name": note.get("SourceName"),
                                "id": note.get("SourceId"),
                            },
                            "link": note.get("Link"),
                        },
                    )

                    # --- Synthesize status from certain messages (with recency guard) ---
                    msg_l = (msg or "").lower()

                    def _emit_status(payload: dict[str, Any]) -> None:
                        if self._recent_real_status(did, window=1.0):
                            _LOGGER.debug(
                                "[%s] Skip synth for door_id=%s (recent real status) payload=%s",
                                self.entry_id, did, payload
                            )
                            return
                        _LOGGER.debug("[%s] Synth door status -> door_id=%s payload=%s",
                                      self.entry_id, did, payload)
                        async_dispatcher_send(
                            self.hass,
                            f"{DISPATCH_DOOR}_{self.entry_id}",
                            {"door_id": did, "status": payload},
                        )

                    if "has been overridden" in msg_l and "current state is" in msg_l:
                        m = re.search(r"current state is\s+([a-z\s/]+)", msg_l)
                        mode_txt = (m.group(1).strip() if m else "")
                        modes_ordered = [
                            (r"\bcard\s+or\s+pin\b", 3),
                            (r"\bcard\s+and\s+pin\b", 4),
                            (r"\bfirst\s+credential\s+in\b", 6),
                            (r"\bdual\s+credential\b", 7),
                            (r"\blockdown\b", 0),
                            (r"\bunlock(?:ed)?\b", 5),
                            (r"\bpin\b", 2),
                            (r"\bcard\b", 1),
                        ]
                        tz = None
                        for pat, val in modes_ordered:
                            if re.search(pat, mode_txt):
                                tz = val
                                break
                        payload = {"overridden": True}
                        if tz is not None:
                            payload["timeZone"] = tz
                            if tz == 5:
                                payload["strike"] = True
                                payload["opener"] = True
                        _emit_status(payload)

                    elif ("unlock until resume" in msg_l
                          or "unlock until next schedule" in msg_l
                          or "timed override unlock" in msg_l):
                        _emit_status({"strike": True, "opener": True, "overridden": True, "timeZone": 5})

                    elif ("cardorpin until resume" in msg_l
                          or "card or pin until resume" in msg_l):
                        _emit_status({"overridden": True, "timeZone": 3})

                    elif (
                        "resume schedule" in msg_l
                        or "schedule resumed" in msg_l
                        or "returned to schedule" in msg_l
                        or "override cleared" in msg_l
                        or "has resumed from an overridden state" in msg_l
                    ):
                        restore_tz = self._baseline_reader_tz.get(did, 1)
                        _emit_status({"overridden": False, "timeZone": restore_tz})

                    if ntype == "DOOR_LOCK_STATE":
                        if "unlocked" in msg_l:
                            _emit_status({"strike": True, "opener": True})
                        elif "locked" in msg_l:
                            _emit_status({"strike": False, "opener": False})

                    # --- DOOR_CONTACT_STATE: physical door open/closed/held-open ---
                    # Hartmann sends one of:
                    #   StateValues = "OPEN"            "is Now Open"
                    #   StateValues = "CLOSED"          "is Now Closed"
                    #   StateValues = "HELD_OPEN"       "Is Being Held Open"   (Odyssey only)
                    #   StateValues = "FORCED_OPEN"     opened without auth    (Odyssey only)
                    #   StateValues = "FORCED_OPEN_ENDED"   forced state cleared (Odyssey only)
                    #
                    # Polarity is already applied by Hartmann — we use the value
                    # directly. This is the SOLE state source on panels that
                    # don't push raw `Input` status frames for the contact, so
                    # we drive the binary_sensor from here in addition to (or
                    # in place of) the input-status path above.
                    #
                    # Protector.Net legacy quirk: it does NOT emit HELD_OPEN
                    # over SignalR. Its web UI derives the "Held Open" badge
                    # purely client-side from contact on-time vs. the door's
                    # AllowedHeldOpenTime config. To match parity, on every
                    # OPEN we start a per-door timer (see _start_held_open_timer)
                    # that synthesizes a HELD_OPEN dispatch when the threshold
                    # elapses. On CLOSED we cancel it. On a real HELD_OPEN
                    # notif (Odyssey) we cancel the timer too — the panel
                    # already told us, no need to synthesize.
                    if ntype == "DOOR_CONTACT_STATE":
                        sv_raw = note.get("StateValues")
                        sv = (str(sv_raw or "").strip().upper()
                              .replace(" ", "_").replace("-", "_"))
                        # Some panels send "HELDOPEN" with no underscore; normalize.
                        if sv == "HELDOPEN":
                            sv = "HELD_OPEN"
                        if sv == "FORCEDOPEN":
                            sv = "FORCED_OPEN"
                        if sv in ("FORCED_OPEN_ENDED", "FORCEDOPENENDED"):
                            sv = "FORCED_OPEN_ENDED"

                        # Treat FORCED_OPEN like HELD_OPEN/OPEN for is_open
                        # purposes — the door is physically open in both
                        # cases. We expose the raw state in `state_values`
                        # so automations can branch on the security event.
                        # FORCED_OPEN_ENDED means the security alarm cleared
                        # (door was closed); treat as a CLOSED transition
                        # for is_open, but don't lie about state_values.
                        if sv in ("OPEN", "CLOSED", "HELD_OPEN",
                                  "FORCED_OPEN", "FORCED_OPEN_ENDED"):
                            is_open = sv in ("OPEN", "HELD_OPEN", "FORCED_OPEN")
                            held_open = (sv == "HELD_OPEN")

                            # Cache so a freshly-added/restored entity can
                            # seed itself without waiting for the next event.
                            cfg_bucket = (self.hass.data.get(DOMAIN, {})
                                          .get(self.entry_id, {}))
                            ds_cache = cfg_bucket.get(KEY_DOOR_CONTACT_STATE_CACHE)
                            if isinstance(ds_cache, dict):
                                ds_cache[did] = {
                                    "is_open":    is_open,
                                    "held_open":  held_open,
                                    "ts":         note.get("Date"),
                                    "state_values": sv,
                                }

                            # Manage the held-open timer based on transition.
                            # OPEN → start (so Protector.Net fires synth at threshold)
                            # CLOSED / FORCED_OPEN_ENDED → cancel (door closed)
                            # HELD_OPEN → cancel (panel beat us to it; don't double-fire)
                            # FORCED_OPEN → leave timer alone (door is open and
                            #   may still cross the held-open threshold; Hartmann
                            #   reports forced-open and held-open as orthogonal)
                            if sv == "OPEN":
                                self._start_held_open_timer(did, note.get("Date"))
                            elif sv in ("CLOSED", "FORCED_OPEN_ENDED", "HELD_OPEN"):
                                self._cancel_held_open_timer(did)

                            async_dispatcher_send(
                                self.hass,
                                f"{DISPATCH_DOOR_CONTACT}_{self.entry_id}",
                                {
                                    "source":       "notification",
                                    "door_id":      did,
                                    "is_open":      is_open,
                                    "held_open":    held_open,
                                    "state_values": sv,
                                    "ts":           note.get("Date"),
                                },
                            )

                    # --- DOOR_CONTACT_INPUT_STATE: raw contact input ON/OFF ---
                    # Protector.Net (legacy) emits this notification alongside
                    # DOOR_CONTACT_STATE on every contact transition:
                    #   StateValues = "ON"   Message: "Door <name> Door Contact is On"
                    #   StateValues = "OFF"  Message: "Door <name> Door Contact is Off"
                    #
                    # In observed deployments contact-on==door-open (Hartmann
                    # has already applied the input's IsInverted polarity, so
                    # ON/OFF is the *logical* state, same convention as
                    # DOOR_CONTACT_STATE OPEN/CLOSED). We use it as a defensive
                    # backup signal — if a transient SignalR drop loses the
                    # DOOR_CONTACT_STATE frame but DOOR_CONTACT_INPUT_STATE
                    # still arrives (or vice versa), the binary_sensor still
                    # tracks reality. Held-open timer is also driven from here
                    # so the workaround stays robust if Protector.Net ever
                    # stops emitting DOOR_CONTACT_STATE for some panel rev.
                    #
                    # Odyssey panels we've seen don't emit this notification,
                    # so this branch is effectively a no-op there.
                    elif ntype == "DOOR_CONTACT_INPUT_STATE":
                        sv_raw = note.get("StateValues")
                        sv = str(sv_raw or "").strip().upper()
                        if sv in ("ON", "OFF"):
                            is_open = (sv == "ON")
                            cfg_bucket = (self.hass.data.get(DOMAIN, {})
                                          .get(self.entry_id, {}))
                            ds_cache = cfg_bucket.get(KEY_DOOR_CONTACT_STATE_CACHE)

                            # Preserve held_open across an INPUT_STATE-only
                            # transition: if the door was already in held_open
                            # state, an OFF→ON wouldn't (and shouldn't) clear
                            # it. Closing always clears.
                            prev = (ds_cache.get(did) if isinstance(ds_cache, dict)
                                    else None) or {}
                            held_open = bool(prev.get("held_open")) if is_open else False

                            if isinstance(ds_cache, dict):
                                ds_cache[did] = {
                                    "is_open":     is_open,
                                    "held_open":   held_open,
                                    "ts":          note.get("Date"),
                                    "state_values": sv,
                                }

                            # Mirror the timer logic from DOOR_CONTACT_STATE
                            # so this branch alone is sufficient on panels
                            # that emit only INPUT_STATE.
                            if is_open:
                                # Only (re)start the timer if not already
                                # held — otherwise we'd push a held door
                                # back to "merely open" for the duration.
                                if not held_open:
                                    self._start_held_open_timer(did, note.get("Date"))
                            else:
                                self._cancel_held_open_timer(did)

                            async_dispatcher_send(
                                self.hass,
                                f"{DISPATCH_DOOR_CONTACT}_{self.entry_id}",
                                {
                                    "source":       "notification",
                                    "door_id":      did,
                                    "is_open":      is_open,
                                    "held_open":    held_open,
                                    "state_values": sv,
                                    "ts":           note.get("Date"),
                                },
                            )

                    _LOGGER.debug("[%s] Routed notification to door_id=%s: %s",
                                  self.entry_id, did, msg)
                    self._push_hub_state()
                continue  # handled

            # else: keep-alives / completion / pings are ignored

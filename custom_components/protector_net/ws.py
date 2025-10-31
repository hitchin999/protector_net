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
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DOMAIN

_LOGGER = logging.getLogger(f"{DOMAIN}.ws")

SIGNALR_RS = "\x1e"  # record separator
DISPATCH_DOOR = f"{DOMAIN}_door_event"        # sensors/switch/select listen on f"{...}_{entry_id}"
DISPATCH_HUB  = f"{DOMAIN}_hub_event"
DISPATCH_LOG  = f"{DOMAIN}_door_log"          # Last Door Log sensor


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

        # Partition-scoped allowlist and maps
        self._allowed_door_ids: Set[int] = set()         # doors in THIS entry’s partition
        self._door_map: Dict[str, Tuple[int, str]] = {}  # statusId -> (door_id, name) [filtered]
        self._name_index: Dict[str, int] = {}            # normalized door name -> door_id [filtered]
        self._reader_by_id: dict[int, int] = {}          # reader_id -> door_id [filtered]
        self._reader_by_name: dict[str, int] = {}        # reader_name(lower/variants) -> door_id [filtered]
        self._last_door_status_at: dict[int, float] = {}
        self._baseline_reader_tz: dict[int, int] = {}    # door_id -> last known non-overridden timeZone

        # Actively managed handles for clean shutdown
        self._session: ClientSession | None = None
        self._ws: Any | None = None

        # Hub status attrs (exposed via hub sensor)
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

    # Public -----------------------------------------------------------------

    def async_start(self) -> None:
        """Start the background websocket task."""
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = self.hass.async_create_task(self._runner())

    async def async_stop(self) -> None:
        """Stop the WS client quickly and deterministically so HA can reload."""
        self._stop.set()

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
            _LOGGER.error("[%s] Failed to fetch allowed doors: %s", self.entry_id, e)
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
            _LOGGER.error("[%s] Failed to fetch system overview: %s", self.entry_id, e)
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
            _LOGGER.error("[%s] Failed to fetch partition readers: %s", self.entry_id, e)

        merged = 0
        if extra_readers:
            for rd in extra_readers:
                rid = rd.get("Id")
                door_id_from_api = rd.get("DoorId")
                rname = (rd.get("Name") or "").strip()

                if not isinstance(rid, int) or not isinstance(door_id_from_api, int):
                    continue

                # keep it partition-scoped
                if self._allowed_door_ids and door_id_from_api not in self._allowed_door_ids:
                    continue

                # 1) id → door
                self._reader_by_id[rid] = door_id_from_api

                # 2) name → door
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

    async def _negotiate(self, session: ClientSession, base: str, cookie: str) -> str:
        url = f"{base}/rt/notificationHub/negotiate?negotiateVersion=1"
        headers = {"Cookie": cookie, "Content-Type": "text/plain"}
        async with session.post(url, headers=headers, data=b"") as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data["connectionToken"]

    async def _send_invocation(self, ws, target: str, args: list[Any], inv_id: str) -> None:
        frame = {
            "type": 1,
            "target": target,
            "arguments": args,
            "invocationId": inv_id,
            "streamIds": [],
        }
        await ws.send_str(json.dumps(frame) + SIGNALR_RS)

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

    # ---------- Runner / message loop ----------

    async def _runner(self) -> None:
        """Main websocket loop."""
        cfg = self.hass.data[DOMAIN][self.entry_id]
        base: str = cfg["base_url"]
        cookie: str = f"ss-id={cfg['session_cookie']}"
        verify_ssl: bool = bool(cfg.get("verify_ssl", False))

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
                    try:
                        await self._build_door_map()

                        token = await self._negotiate(session, base, cookie)
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
                        _LOGGER.error("[%s] WS error: %s", self.entry_id, e)
                        self._push_hub_state()

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
                        tz_val = st.get("timeZone", None)
                        ov_val = st.get("overridden", None)
                        if tz_val is not None and not ov_val:
                            try:
                                tz_int = int(tz_val)
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

                    async_dispatcher_send(
                        self.hass,
                        f"{DISPATCH_DOOR}_{self.entry_id}",
                        {"door_id": door_id, "status": st},
                    )
                    self._push_hub_state()
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

                allowed_door_ids = self._allowed_door_ids or {did for (_sid, (did, _)) in self._door_map.items()}

                for note in notes_iter:
                    msg = note.get("Message") or ""
                    ntype = (note.get("NotificationType") or "").upper()

                    did = self._door_id_from_notification(note)
                    
                    if did is None:
                        # Mute noisy ActionPlan state chatter with no door routing
                        if ntype.startswith("ACTIONPLAN_"):
                            continue
                        _LOGGER.debug("[%s] Unmapped notification: %s", self.entry_id, note)
                        self._push_hub_state()
                        continue
                    
                    # partition guard
                    if allowed_door_ids and did not in allowed_door_ids:
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

                    # 1) Structured override line: "<Door> has been overridden. Current state is <Mode>"
                    if "has been overridden" in msg_l and "current state is" in msg_l:
                        m = re.search(r"current state is\s+([a-z\s/]+)", msg_l)
                        mode_txt = (m.group(1).strip() if m else "")

                        # Longest-first, word-boundary matching to avoid
                        # "card" matching inside "card or pin" / "card and pin".
                        modes_ordered = [
                            (r"\bcard\s+or\s+pin\b", 3),
                            (r"\bcard\s+and\s+pin\b", 4),
                            (r"\bfirst\s+credential\s+in\b", 6),
                            (r"\bdual\s+credential\b", 7),
                            (r"\blockdown\b", 0),       # 0 for lockdown
                            (r"\bunlock(?:ed)?\b", 5),  # unlock/unlocked
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

                    # 2) Our action-plan phrasing from HA buttons
                    elif ("unlock until resume" in msg_l
                          or "unlock until next schedule" in msg_l
                          or "timed override unlock" in msg_l):
                        _emit_status({"strike": True, "opener": True, "overridden": True, "timeZone": 5})

                    elif ("cardorpin until resume" in msg_l
                          or "card or pin until resume" in msg_l):
                        _emit_status({"overridden": True, "timeZone": 3})

                    # 3) Resume/clear override messages
                    elif (
                        "resume schedule" in msg_l
                        or "schedule resumed" in msg_l
                        or "returned to schedule" in msg_l
                        or "override cleared" in msg_l
                        or "has resumed from an overridden state" in msg_l
                    ):
                        restore_tz = self._baseline_reader_tz.get(did, 1)
                        _emit_status({"overridden": False, "timeZone": restore_tz})

                    # 4) Explicit lock-state notifications keep Lock State in sync
                    if ntype == "DOOR_LOCK_STATE":
                        if "unlocked" in msg_l:
                            _emit_status({"strike": True, "opener": True})
                        elif "locked" in msg_l:
                            _emit_status({"strike": False, "opener": False})

                    _LOGGER.debug("[%s] Routed notification to door_id=%s: %s",
                                  self.entry_id, did, msg)
                    self._push_hub_state()
                continue  # handled

            # else: keep-alives / completion / pings are ignored

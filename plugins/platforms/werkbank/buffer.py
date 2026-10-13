"""Ring buffer per ``session_key``: epoch, gap-free ``seq``, replay after a reconnect.

Only sessions a client asked for through ``/v1/resolve`` are buffered (:meth:`Buffers.watch`); every
other session's events are dropped on arrival. Texts live only here, in memory — never in a log.

Epochs. ``seq`` runs 1, 2, 3, … per ``(session_key, epoch)``. A new epoch starts — the buffer is
emptied and ``seq`` restarts at 0 — when the plugin can no longer promise a gap-free stream:

* ``overflow``     the core dropped events for this subscriber (``gateway/session_events.py``);
* ``luecke``       a hole in the core's ``core_seq`` (an event got lost before it reached us);
* ``session_id``   the session behind the key changed (``/new``, compression);
* plugin restart   the buffers are in memory, so a new process starts new epochs. Epoch numbers are
  drawn from one process-wide counter seeded from the clock (``initial_epoch``): an epoch number a
  client kept from before a restart does not match a new one.

A stream client then sees exactly one ``session.epoch`` event and reloads the snapshot.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone
from itertools import islice
from typing import Any, Callable, Dict, Iterable, List, Optional

RING = 5000                 # events kept per session (contract, plan section 2)
MAX_SESSIONS = 64           # watched sessions; the least recently used one is dropped beyond it
_EPOCH_ORIGIN = 1_700_000_000
_PENDING_IDS = 256          # client_message_ids waiting for their user.message (pre WB-1127 cores)
_ACCEPTED_IDS = 2048        # client_message_ids already accepted per session (409 on a repeat)
_RECENT_USERS = 200         # user.message events kept for the snapshot (rows not yet in the DB)
_RECENT_ANSWERS = 20        # final answers of turn.done kept for the snapshot


def initial_epoch(now: Optional[float] = None) -> int:
    """First epoch number of this process: 1000 per second since 2023-11 (<= 12 digits until ~2050)."""
    seconds = int(time.time() if now is None else now)
    return max(1, (seconds - _EPOCH_ORIGIN) * 1000)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class SessionBuffer:
    """State of one watched session. Only touched under :attr:`Buffers.cond`."""

    def __init__(self, session_key: str, session_id: Optional[str], platform: Optional[str],
                 epoch: int, ring: int) -> None:
        self.session_key = session_key
        self.session_id = session_id
        self.platform = platform
        self.epoch = epoch
        self.epoch_reason = "start"
        self.seq = 0
        self.events: deque = deque(maxlen=ring)
        self.last_core_seq: Optional[int] = None
        self.known_ids = {session_id} if session_id else set()
        self.open_turn: Optional[str] = None
        self.subagents: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self.pending_ids: deque = deque(maxlen=_PENDING_IDS)
        self.accepted_ids: "OrderedDict[str, None]" = OrderedDict()
        self.model: Optional[str] = None
        # Survive epochs: what a snapshot must show even before the SessionDB has it.
        self.recent_users: deque = deque(maxlen=_RECENT_USERS)
        self.recent_answers: deque = deque(maxlen=_RECENT_ANSWERS)
        self.tool_count = 0
        self.error_count = 0

    # -- seq window ---------------------------------------------------------------------------
    @property
    def first_seq(self) -> int:
        return self.events[0]["seq"] if self.events else self.seq + 1

    def after(self, after: int) -> Optional[List[Dict[str, Any]]]:
        """Events with ``seq > after`` of the current epoch, or ``None`` when *after* is not in the
        buffer any more (or not yet)."""
        first = self.first_seq
        if after < first - 1 or after > self.seq:
            return None
        return list(islice(self.events, after - first + 1, None))


class Buffers:
    """All watched sessions plus the condition stream readers wait on."""

    def __init__(self, *, plugin_id: str = "platforms/werkbank", ring: int = RING,
                 max_sessions: int = MAX_SESSIONS, epoch_start: Optional[int] = None,
                 clock: Callable[[], str] = _now) -> None:
        self.plugin_id = plugin_id
        self.ring = ring
        self.max_sessions = max_sessions
        self.cond = threading.Condition()
        self._sessions: "OrderedDict[str, SessionBuffer]" = OrderedDict()
        self._next_epoch = initial_epoch() if epoch_start is None else epoch_start
        self._clock = clock
        self.closed = False

    # -- watching -----------------------------------------------------------------------------
    def _take_epoch(self) -> int:
        epoch = self._next_epoch
        self._next_epoch += 1
        return epoch

    def watch(self, session_key: str, session_id: Optional[str], platform: Optional[str]) -> SessionBuffer:
        """Start (or keep) buffering *session_key*; a changed session id starts a new epoch."""
        with self.cond:
            buf = self._sessions.get(session_key)
            if buf is None:
                buf = SessionBuffer(session_key, session_id, platform, self._take_epoch(), self.ring)
                self._sessions[session_key] = buf
                while len(self._sessions) > self.max_sessions:
                    self._sessions.popitem(last=False)
                self.cond.notify_all()
            else:
                self._sessions.move_to_end(session_key)
                if platform:
                    buf.platform = platform
                if session_id and session_id != buf.session_id:
                    self._new_epoch(buf, "session_id")
                    buf.session_id = session_id
                    buf.known_ids.add(session_id)
            return buf

    def get(self, session_key: str) -> Optional[SessionBuffer]:
        with self.cond:
            return self._sessions.get(session_key)

    def key_for_session_id(self, session_id: str) -> Optional[str]:
        """The watched key that once ran *session_id* (a board with a stale id after ``/new``)."""
        with self.cond:
            for key, buf in self._sessions.items():
                if session_id in buf.known_ids:
                    return key
        return None

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    # -- epochs -------------------------------------------------------------------------------
    def _new_epoch(self, buf: SessionBuffer, reason: str) -> None:
        buf.epoch = self._take_epoch()
        buf.epoch_reason = reason
        buf.seq = 0
        buf.events.clear()
        self.cond.notify_all()

    def new_epoch(self, session_key: str, reason: str) -> Optional[int]:
        with self.cond:
            buf = self._sessions.get(session_key)
            if buf is None:
                return None
            self._new_epoch(buf, reason)
            return buf.epoch

    def epoch_event(self, buf: SessionBuffer, grund: str) -> Dict[str, Any]:
        """The ``session.epoch`` event of *buf*'s current epoch (``seq`` = its current end)."""
        return {"v": 1, "session_key": buf.session_key, "session_id": buf.session_id,
                "epoch": buf.epoch, "seq": buf.seq, "type": "session.epoch", "id": None,
                "parent_id": None, "origin": "werkbank", "platform": buf.platform,
                "core_seq": None, "ts": self._clock(),
                "payload": {"grund": grund, "ursache": buf.epoch_reason}}

    # -- messages sent from the board ---------------------------------------------------------
    def accept_id(self, session_key: str, client_message_id: str, *, awaits_event: bool) -> bool:
        """Reserve *client_message_id* for a send; False when it was already accepted (409).

        ``awaits_event``: the core cannot carry the id (no ``client_message_id`` on
        ``inject_message``), so the next ``user.message`` this plugin injected gets it here."""
        with self.cond:
            buf = self._sessions.get(session_key)
            if buf is None or client_message_id in buf.accepted_ids:
                return False
            buf.accepted_ids[client_message_id] = None
            while len(buf.accepted_ids) > _ACCEPTED_IDS:
                buf.accepted_ids.popitem(last=False)
            if awaits_event:
                buf.pending_ids.append(client_message_id)
            return True

    def release_id(self, session_key: str, client_message_id: str) -> None:
        """Undo :meth:`accept_id` after the gateway refused the message (the board may resend)."""
        with self.cond:
            buf = self._sessions.get(session_key)
            if buf is None:
                return
            buf.accepted_ids.pop(client_message_id, None)
            try:
                buf.pending_ids.remove(client_message_id)
            except ValueError:
                pass

    # -- the hook -----------------------------------------------------------------------------
    def on_event(self, event: Any) -> None:
        """``on_gateway_session_event`` subscriber. Never raises, never logs a text."""
        if not isinstance(event, dict):
            return
        key = event.get("session_key")
        if not isinstance(key, str):
            return
        with self.cond:
            buf = self._sessions.get(key)
            if buf is None or self.closed:
                return                       # not asked for by the board: not buffered
            type_ = str(event.get("type") or "")
            payload = dict(event.get("payload") or {})
            if type_ == "overflow":
                self._new_epoch(buf, "overflow")
                last = payload.get("last_seq")
                buf.last_core_seq = last if isinstance(last, int) else None
                return
            core_seq = event.get("core_seq")
            if isinstance(core_seq, int):
                if buf.last_core_seq is not None and core_seq != buf.last_core_seq + 1:
                    self._new_epoch(buf, "luecke")
                buf.last_core_seq = core_seq
            session_id = event.get("session_id")
            if session_id and session_id != buf.session_id:
                if buf.session_id is not None:
                    self._new_epoch(buf, "session_id")
                buf.session_id = str(session_id)
                buf.known_ids.add(buf.session_id)
            if event.get("platform"):
                buf.platform = str(event["platform"])
            id_ = event.get("id")
            if (type_ == "user.message" and not payload.get("client_message_id")
                    and event.get("origin") == self.plugin_id and buf.pending_ids):
                # A core without ``inject_message(client_message_id=…)``: the id rides here.
                id_ = buf.pending_ids.popleft()
                payload["client_message_id"] = id_
            origin = event.get("origin")
            if origin == self.plugin_id:
                origin = "werkbank"          # the board's name for itself (hermes_view.js HV_ORIGINS)
            self._track(buf, type_, id_, event.get("parent_id"), payload)
            ts = event.get("ts") or self._clock()
            if type_ == "user.message":
                buf.recent_users.append({
                    "id": None if id_ is None else str(id_),
                    "client_message_id": payload.get("client_message_id"),
                    "message_id": payload.get("message_id"), "text": str(payload.get("text") or ""),
                    "origin": origin, "ts": ts, "delivery": payload.get("delivery"),
                    "synthetic": payload.get("synthetic")})
            elif type_ == "turn.done" and payload.get("final_response"):
                buf.recent_answers.append({"turn_id": None if id_ is None else str(id_),
                                           "text": str(payload["final_response"]), "ts": ts})
            buf.seq += 1
            buf.events.append({
                "v": 1, "session_key": key, "session_id": buf.session_id, "epoch": buf.epoch,
                "seq": buf.seq, "type": type_, "id": id_, "parent_id": event.get("parent_id"),
                "origin": origin, "platform": event.get("platform"),
                "core_seq": core_seq if isinstance(core_seq, int) else None,
                "ts": ts, "payload": payload,
            })
            self.cond.notify_all()

    @staticmethod
    def _track(buf: SessionBuffer, type_: str, id_: Any, parent_id: Any, payload: Dict[str, Any]) -> None:
        if type_ == "tool.start":
            buf.tool_count += 1
        elif type_ == "tool.end" and payload.get("is_error"):
            buf.error_count += 1
        elif type_ == "error":
            buf.error_count += 1
        if type_ in ("turn.done", "turn.stopped", "error") and payload.get("model"):
            buf.model = str(payload["model"])
        if type_ == "turn.start":
            buf.open_turn = None if id_ is None else str(id_)
        elif type_ in ("turn.done", "turn.stopped") or (type_ == "error" and id_ == buf.open_turn):
            buf.open_turn = None
        elif type_ == "subagent.start" and id_ is not None:
            buf.subagents[str(id_)] = {"id": str(id_), "parent_id": parent_id, "status": "laeuft",
                                       **{k: payload[k] for k in ("goal", "model", "depth",
                                                                   "child_session_id") if k in payload}}
            while len(buf.subagents) > 64:
                buf.subagents.popitem(last=False)
        elif type_ == "subagent.end" and id_ is not None:
            buf.subagents.pop(str(id_), None)

    # -- for the snapshot -----------------------------------------------------------------------
    def position(self, session_key: str) -> Optional[Dict[str, Any]]:
        """Epoch, seq and the session state the buffer knows, read under one lock."""
        with self.cond:
            buf = self._sessions.get(session_key)
            if buf is None:
                return None
            return {"epoch": buf.epoch, "seq": buf.seq, "turn_id": buf.open_turn,
                    "subagents": [dict(s) for s in buf.subagents.values()],
                    "session_id": buf.session_id, "platform": buf.platform,
                    "recent_users": [dict(u) for u in buf.recent_users],
                    "recent_answers": [dict(a) for a in buf.recent_answers],
                    # counted since the board first asked for this session (this plugin process)
                    "session": {"model": buf.model, "tool_count": buf.tool_count,
                                "errors": buf.error_count}}

    def sessions(self) -> Iterable[str]:
        with self.cond:
            return list(self._sessions)

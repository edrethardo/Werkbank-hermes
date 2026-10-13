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

Row marks. What the snapshot adds from this buffer (an announced message, a final answer) must
not double a SessionDB row that already stands for it. The buffer never compares clocks or texts:
it notes the highest SessionDB row id of the session (``watermark``, ``messages.id`` is
AUTOINCREMENT and only grows) when it first observes a session id and when a ``user.message``,
``turn.start`` or ``turn.done`` arrives. Every kept entry carries that mark and its ``session_id``.

Buffered messages and answers only bridge the time until their SessionDB row exists. Once a row was
seen for an entry (:meth:`Buffers.confirm`, called by the snapshot) or the core reported the
answer's row as written (``turn.done`` ``row_id`` at or below the session's highest row id), the
entry leaves the buffer for good — a later compression or ``/undo`` that takes the row out of the
active history never brings the buffered copy back. A confirmed answer also drops every older
unconfirmed one (its turn ended before), and a new session id drops the answers of the old one.
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
_TURN_STARTS = 200          # turn.start row marks kept: which answer row belongs to which turn
_OBSERVED = 64              # session ids of one key with their observation mark
_MARKED = ("user.message", "turn.start", "turn.done")


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
        # session_id -> highest row id when this plugin first saw that id (turns begun before it)
        self.observed: "OrderedDict[str, Optional[int]]" = OrderedDict()
        self.order = 0
        self.open_turn: Optional[str] = None
        self.subagents: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self.pending_ids: deque = deque(maxlen=_PENDING_IDS)
        self.accepted_ids: "OrderedDict[str, None]" = OrderedDict()
        self.model: Optional[str] = None
        # Survive epochs: what a snapshot must show even before the SessionDB has it.
        self.recent_users: deque = deque(maxlen=_RECENT_USERS)
        self.recent_answers: deque = deque(maxlen=_RECENT_ANSWERS)
        # turn_id -> {session_id, mark} of its turn.start, in arrival order (row ids, never the clock)
        self.turn_starts: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self.confirmed_order = 0         # order of the newest answer whose row was confirmed
        self.readers = 0                 # open event streams on this session (never evicted while > 0)
        self.evicted = False
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
                 clock: Callable[[], str] = _now,
                 watermark: Optional[Callable[[str, Optional[str]], Optional[int]]] = None) -> None:
        self._watermark = watermark
        self.plugin_id = plugin_id
        self.ring = ring
        self.max_sessions = max_sessions
        self.cond = threading.Condition()
        self._sessions: "OrderedDict[str, SessionBuffer]" = OrderedDict()
        self._next_epoch = initial_epoch() if epoch_start is None else epoch_start
        self._clock = clock
        self.closed = False

    # -- row marks ----------------------------------------------------------------------------
    def _mark(self, session_key: str, session_id: Optional[str]) -> Optional[int]:
        """Highest SessionDB row id of *session_id* now; ``None`` when it cannot be read. Called
        outside :attr:`cond` (a database read)."""
        if self._watermark is None or not session_id:
            return None
        try:
            mark = self._watermark(session_key, session_id)
        except Exception:
            return None
        return mark if isinstance(mark, int) and not isinstance(mark, bool) else None

    @staticmethod
    def _observe(buf: SessionBuffer, session_id: Optional[str], mark: Optional[int]) -> None:
        if session_id and session_id not in buf.observed:
            buf.observed[session_id] = mark
            while len(buf.observed) > _OBSERVED:
                buf.observed.popitem(last=False)

    # -- confirmed entries leave for good -------------------------------------------------------
    @staticmethod
    def _keep_answers(buf: SessionBuffer, keep: Callable[[Dict[str, Any]], bool]) -> None:
        kept = [a for a in buf.recent_answers if keep(a)]
        if len(kept) != len(buf.recent_answers):
            buf.recent_answers = deque(kept, maxlen=_RECENT_ANSWERS)

    def _answer_confirmed(self, buf: SessionBuffer, order: int) -> None:
        """The answer of arrival *order* has its row: it and every older answer leave the buffer."""
        buf.confirmed_order = max(buf.confirmed_order, order)
        self._keep_answers(buf, lambda a: (a.get("order") or 0) > buf.confirmed_order)

    def _session_changed(self, buf: SessionBuffer) -> None:
        """A new session id (compression rotation, ``/new``): answers of the old id are history."""
        self._keep_answers(buf, lambda a: a.get("session_id") == buf.session_id)

    def confirm(self, session_key: str, *, answers: Iterable[int] = (), users: Iterable[int] = ()) -> None:
        """The snapshot saw the rows of these entries (by arrival ``order``): drop them for good."""
        answers, users = set(answers), set(users)
        if not answers and not users:
            return
        with self.cond:
            buf = self._sessions.get(session_key)
            if buf is None:
                return
            if users:
                kept = [u for u in buf.recent_users if u.get("order") not in users]
                buf.recent_users = deque(kept, maxlen=_RECENT_USERS)
            if answers:
                self._answer_confirmed(buf, max(answers))

    # -- watching -----------------------------------------------------------------------------
    def _take_epoch(self) -> int:
        epoch = self._next_epoch
        self._next_epoch += 1
        return epoch

    def watch(self, session_key: str, session_id: Optional[str], platform: Optional[str]) -> SessionBuffer:
        """Start (or keep) buffering *session_key*; a changed session id starts a new epoch."""
        with self.cond:
            buf = self._sessions.get(session_key)
            fresh = buf is None or (session_id is not None and session_id not in buf.observed)
        mark = self._mark(session_key, session_id) if fresh else None
        with self.cond:
            buf = self._sessions.get(session_key)
            if buf is None:
                buf = SessionBuffer(session_key, session_id, platform, self._take_epoch(), self.ring)
                self._observe(buf, session_id, mark)
                self._sessions[session_key] = buf
                self._evict(keep=session_key)
                self.cond.notify_all()
            else:
                self._sessions.move_to_end(session_key)
                if platform:
                    buf.platform = platform
                if session_id and session_id != buf.session_id:
                    self._new_epoch(buf, "session_id")
                    buf.session_id = session_id
                    buf.known_ids.add(session_id)
                    self._session_changed(buf)
                if fresh:
                    self._observe(buf, session_id, mark)
            return buf

    def _evict(self, keep: str) -> None:
        """Drop least recently used sessions beyond ``max_sessions``: first those nobody streams;
        a streamed one only when nothing else is left — its stream then ends with ``session.epoch``."""
        while len(self._sessions) > self.max_sessions:
            idle = [k for k, b in self._sessions.items() if k != keep and b.readers <= 0]
            victims = idle or [k for k in self._sessions if k != keep]
            if not victims:
                return
            self._sessions.pop(victims[0]).evicted = True

    def reader(self, delta: int, session_key: str) -> None:
        """Count an event stream opening (+1) or closing (-1) on *session_key*."""
        with self.cond:
            buf = self._sessions.get(session_key)
            if buf is not None:
                buf.readers = max(0, buf.readers + delta)

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
        type_ = str(event.get("type") or "")
        event_sid = str(event["session_id"]) if event.get("session_id") else None
        with self.cond:
            buf = self._sessions.get(key)
            wants_mark = buf is not None and (type_ in _MARKED or (event_sid is not None
                                                                   and event_sid not in buf.observed))
        mark = self._mark(key, event_sid or (buf.session_id if buf else None)) if wants_mark else None
        with self.cond:
            buf = self._sessions.get(key)
            if buf is None or self.closed:
                return                       # not asked for by the board: not buffered
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
                self._session_changed(buf)
            self._observe(buf, buf.session_id, mark)
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
            sid = buf.session_id
            if type_ in _MARKED:
                buf.order += 1                 # arrival order of marked events (never a clock)
            if type_ == "user.message":
                buf.recent_users.append({
                    "id": None if id_ is None else str(id_),
                    "client_message_id": payload.get("client_message_id"),
                    "message_id": payload.get("message_id"), "text": str(payload.get("text") or ""),
                    "origin": origin, "ts": ts, "delivery": payload.get("delivery"),
                    "synthetic": payload.get("synthetic"), "session_id": sid, "mark": mark,
                    "order": buf.order})
            if type_ == "turn.start" and id_ is not None:
                buf.turn_starts[str(id_)] = {"session_id": sid, "mark": mark, "order": buf.order}
                while len(buf.turn_starts) > _TURN_STARTS:
                    buf.turn_starts.popitem(last=False)
            elif type_ == "turn.done" and payload.get("final_response"):
                start = buf.turn_starts.get(str(id_)) if id_ is not None else None
                uid, row_id = payload.get("message_uid"), payload.get("row_id")
                row_id = row_id if isinstance(row_id, int) and not isinstance(row_id, bool) else None
                uid = uid if isinstance(uid, str) and uid else None
                if uid and row_id is not None and isinstance(mark, int) and row_id <= mark:
                    # the core wrote the answer's row before turn.done (the session already has it)
                    self._answer_confirmed(buf, buf.order)
                else:
                    self._add_answer(buf, id_, payload, ts, sid, mark, start, uid, row_id)
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
    def _add_answer(buf: SessionBuffer, id_: Any, payload: Dict[str, Any], ts: Any, sid: Optional[str],
                    mark: Optional[int], start: Optional[Dict[str, Any]], uid: Optional[str],
                    row_id: Optional[int]) -> None:
        buf.recent_answers.append({
            "turn_id": None if id_ is None else str(id_), "text": str(payload["final_response"]),
            "ts": ts, "session_id": sid, "mark": mark,
            # the turn's own start, or None: it began before this plugin observed the session
            "start_mark": start["mark"] if start else None, "started": start is not None,
            "order": buf.order,
            # the core named the answer's row (gateway/session_events.end_turn): matched by it alone
            "message_uid": uid, "row_id": row_id})

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
                    "turn_starts": [dict(t) for t in buf.turn_starts.values()],
                    "confirmed_order": buf.confirmed_order,
                    "observed": dict(buf.observed),
                    # counted since the board first asked for this session (this plugin process)
                    "session": {"model": buf.model, "tool_count": buf.tool_count,
                                "errors": buf.error_count}}

    def sessions(self) -> Iterable[str]:
        with self.cond:
            return list(self._sessions)

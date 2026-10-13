"""Ordered per-session event stream of gateway turns for plugins (fork: werkbank-chat).

Every gateway turn — Telegram, Discord, a plugin-injected message — passes the same fire sites
(``gateway/run_turn_runner.py``, the inbound/busy funnels, the approval and clarify primitives).
They call :func:`emit`, which stamps each event with a per-``session_key`` core sequence
``core_seq`` (1, 2, 3, … gap-free for the process lifetime) and hands it to each subscriber of the
``on_gateway_session_event`` plugin hook through that subscriber's own queue and worker thread, so
plugin code never runs on the agent or event-loop thread.

Back-pressure is explicit, never silent: a subscriber queue holds at most :data:`QUEUE_CAP`
events. Events that arrive while it is full are counted per session and, as soon as there is room
again, replaced by ONE ``overflow`` event ``{dropped, first_seq, last_seq}`` that is delivered
before anything that came after the gap. The subscriber sees the hole in ``core_seq`` and the
reason for it, and re-syncs (the Werkbank plugin raises its epoch).

Without a subscriber the cost is one ``has_hook`` lookup per fire site: no payload is built, no
sequence is advanced, no thread exists.

Callback contract (kwargs; narrow signatures receive only what they declare)::

    event        the full event dict below
    session_key, session_id, platform, origin, type, id, parent_id, payload, core_seq, ts

    event = {"v": 1, "session_key", "session_id", "platform", "origin", "core_seq", "type", "id",
             "parent_id", "ts", "payload": {...}}

Event types: ``user.message`` (delivery turn|nachgeschoben|eingereiht, client_message_id),
``turn.start``, ``text.delta``, ``text.interim``, ``tool.start``, ``tool.progress``, ``tool.end``
(output capped to 4 KB, ``truncated``), ``subagent.start|progress|end``, ``status``,
``approval.request|resolved|expired|cancelled``, ``clarify.request|resolved|expired``,
``turn.done``, ``turn.stopped``, ``error`` and ``overflow``. Texts live only in memory here; this
module never logs them.
"""

from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

HOOK = "on_gateway_session_event"
SCHEMA_VERSION = 1
QUEUE_CAP = 10_000  # per subscriber; beyond it an ``overflow`` event replaces the missed span
TEXT_CAP_BYTES = 4096  # tool output / args shown inline; the full text stays in the session DB


# ── subscription gate ───────────────────────────────────────────────────────────────────────


def enabled() -> bool:
    """True when at least one plugin subscribed to :data:`HOOK` (the zero-cost short circuit)."""
    try:
        from hermes_cli.plugins import has_hook
        return bool(has_hook(HOOK))
    except Exception:
        return False


def _callbacks() -> tuple:
    try:
        from hermes_cli.plugins import iter_hook_callbacks
        return tuple(iter_hook_callbacks(HOOK))
    except Exception:
        logger.debug("session event callback lookup failed", exc_info=True)
        return ()


# ── per-subscriber queue ────────────────────────────────────────────────────────────────────


class _Subscriber:
    """One callback's bounded FIFO + worker. ``offer`` never blocks and never drops silently."""

    def __init__(self, callback: Callable[..., Any]) -> None:
        self.callback = callback
        self.cond = threading.Condition()
        self.items: deque = deque()
        self.missed: Dict[str, Dict[str, Any]] = {}  # session_key -> overflow accounting
        self.busy = False
        self.stopped = False
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name=f"gateway-session-events:{getattr(callback, '__name__', 'cb')}")
        self.thread.start()

    def offer(self, context: contextvars.Context, event: Dict[str, Any]) -> None:
        with self.cond:
            self._flush_missed_locked(context)
            if len(self.items) >= QUEUE_CAP or self.missed.get(event["session_key"]):
                miss = self.missed.setdefault(event["session_key"], {
                    "dropped": 0, "first_seq": event["core_seq"], "session_id": event["session_id"],
                    "platform": event["platform"], "origin": event["origin"],
                })
                miss["dropped"] += 1
                miss["last_seq"] = event["core_seq"]
                return
            self.items.append((context, event))
            self.cond.notify()

    def _flush_missed_locked(self, context: contextvars.Context) -> None:
        while self.missed and len(self.items) < QUEUE_CAP:
            key = next(iter(self.missed))
            miss = self.missed.pop(key)
            self.items.append((context, _envelope(
                key, miss["session_id"], miss["platform"], miss["origin"], None, "overflow", None, None,
                {"dropped": miss["dropped"], "first_seq": miss["first_seq"], "last_seq": miss["last_seq"],
                 "queue_cap": QUEUE_CAP},
            )))
            self.cond.notify()

    def _run(self) -> None:
        while True:
            with self.cond:
                while not self.items and not self.stopped:
                    self.cond.wait()
                if self.stopped and not self.items:
                    return
                context, event = self.items.popleft()
                self._flush_missed_locked(context)
                self.busy = True
            try:
                context.copy().run(self._deliver, event)
            finally:
                with self.cond:
                    self.busy = False
                    self.cond.notify_all()

    def _deliver(self, event: Dict[str, Any]) -> None:
        from hermes_cli.plugins import get_plugin_manager
        manager = get_plugin_manager()
        kwargs = {"event": event, **{k: event[k] for k in (
            "session_key", "session_id", "platform", "origin", "type", "id", "parent_id", "payload",
            "core_seq", "ts")}}
        try:
            manager._invoke_hook_callback(self.callback, kwargs)
        except Exception as exc:  # isolated: one broken plugin never stalls another
            try:
                manager._report_hook_failure(HOOK, self.callback, kwargs, exc)
            except Exception:
                logger.debug("session event subscriber failed", exc_info=True)

    def idle(self) -> bool:
        with self.cond:
            return not self.items and not self.missed and not self.busy

    def stop(self) -> None:
        with self.cond:
            self.stopped = True
            self.cond.notify_all()


@dataclass
class _SessionInfo:
    seq: int = 0
    session_id: Optional[str] = None
    platform: Optional[str] = None


_lock = threading.Lock()  # orders core_seq assignment and hand-off: queue order == seq order
_sessions: Dict[str, _SessionInfo] = {}
_subscribers: Dict[int, _Subscriber] = {}


def _subscribers_for(callbacks: tuple) -> list:
    wanted = {id(cb): cb for cb in callbacks}
    for key in [k for k in _subscribers if k not in wanted]:
        _subscribers.pop(key).stop()
    out = []
    for key, cb in wanted.items():
        sub = _subscribers.get(key)
        if sub is None or sub.callback is not cb or not sub.thread.is_alive():
            sub = _subscribers[key] = _Subscriber(cb)
        out.append(sub)
    return out


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _envelope(session_key, session_id, platform, origin, core_seq, type_, id_, parent_id, payload) -> Dict[str, Any]:
    return {
        "v": SCHEMA_VERSION, "session_key": session_key, "session_id": session_id, "platform": platform,
        "origin": origin or platform, "core_seq": core_seq, "type": type_, "id": id_,
        "parent_id": parent_id, "ts": _now(), "payload": dict(payload or {}),
    }


def emit(session_key: Optional[str], session_id: Optional[str], platform: Optional[str], type: str,  # noqa: A002
         id: Optional[str] = None, parent_id: Optional[str] = None,  # noqa: A002
         payload: Optional[Dict[str, Any]] = None, *, origin: Optional[str] = None) -> Optional[int]:
    """Publish one session event; returns its ``core_seq``, or ``None`` when nobody listens.

    ``session_id``/``platform`` may be ``None`` at sites that do not know them (approval wait,
    clarify); the last values seen for ``session_key`` are filled in. Never raises.
    """
    if not session_key or not enabled():
        return None
    try:
        callbacks = _callbacks()
        if not callbacks:
            return None
        context = contextvars.copy_context()
        with _lock:
            info = _sessions.setdefault(session_key, _SessionInfo())
            if session_id:
                info.session_id = str(session_id)
            if platform:
                info.platform = str(platform)
            info.seq += 1
            event = _envelope(session_key, info.session_id, info.platform, origin, info.seq,
                              type, None if id is None else str(id),
                              None if parent_id is None else str(parent_id), payload)
            for sub in _subscribers_for(callbacks):
                sub.offer(context, event)
            return info.seq
    except Exception:
        logger.debug("session event emit failed (type=%s)", type, exc_info=True)
        return None


def wait_idle(timeout: float = 5.0) -> bool:
    """Block until every subscriber delivered everything queued (tests, orderly shutdown)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _lock:
            subs = list(_subscribers.values())
        if all(s.idle() for s in subs):
            return True
        time.sleep(0.005)
    return False


def _reset_for_tests() -> None:
    with _lock:
        subs = list(_subscribers.values())
        _subscribers.clear()
        _sessions.clear()
    for sub in subs:
        sub.stop()
        sub.thread.join(timeout=1.0)


# ── payload helpers (redaction + size caps) ─────────────────────────────────────────────────


def _redact(text: Any) -> str:
    """Egress scrub used for every text leaving the gateway (tool args/output, status lines)."""
    try:
        from agent.redact import redact_for_egress
        return redact_for_egress(str(text))
    except Exception:
        return ""


def redact_command(command: Any) -> str:
    """Approval commands are scrubbed exactly like the Telegram card (``_redact_approval_command``)."""
    try:
        from agent.redact import redact_sensitive_text
        return redact_sensitive_text(str(command or ""), force=True)
    except Exception:
        return ""


def _capped(value: Any, limit: int = TEXT_CAP_BYTES) -> tuple[str, bool]:
    if value is None:
        return "", False
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            value = str(value)
    text = _redact(value)
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode("utf-8", errors="ignore"), True


def _source_platform(source: Any) -> Optional[str]:
    platform = getattr(source, "platform", None)
    return getattr(platform, "value", None) or (str(platform) if platform else None)


def event_origin(event: Any) -> Optional[str]:
    """Who sent it: the injecting plugin's id, else the platform."""
    meta = getattr(event, "metadata", None) or {}
    if meta.get("hermes_plugin_injection") and meta.get("hermes_plugin_id"):
        return str(meta["hermes_plugin_id"])
    return _source_platform(getattr(event, "source", None))


# ── fire-site helpers ───────────────────────────────────────────────────────────────────────


def user_message(session_key: Optional[str], event: Any, delivery: str, *, session_id: Optional[str] = None) -> None:
    """``user.message`` for an admitted (``turn``), steered (``nachgeschoben``) or queued (``eingereiht``) message."""
    if not session_key or not enabled():
        return
    try:
        meta = getattr(event, "metadata", None) or {}
        text = getattr(event, "text", None) or ""
        if delivery == "nachgeschoben" and str(text).lstrip().startswith("/steer"):
            text = event.get_command_args()
        client_message_id = meta.get("client_message_id")
        message_id = getattr(event, "message_id", None)
        emit(session_key, session_id or meta.get("gateway_session_id"), _source_platform(getattr(event, "source", None)),
             "user.message", client_message_id or message_id, None, {
                 "delivery": delivery, "text": str(text),
                 "client_message_id": client_message_id,
                 "message_id": None if message_id is None else str(message_id),
                 "internal": bool(getattr(event, "internal", False)),
                 "media_count": len(getattr(event, "media_urls", None) or []),
             }, origin=event_origin(event))
    except Exception:
        logger.debug("user.message emit failed", exc_info=True)


@dataclass
class TurnScope:
    session_key: str
    session_id: Optional[str]
    platform: Optional[str]
    turn_id: str
    tool_names: Dict[str, str] = field(default_factory=dict)

    def emit(self, type_: str, id_: Optional[str] = None, parent_id: Optional[str] = None,
             payload: Optional[Dict[str, Any]] = None) -> None:
        emit(self.session_key, self.session_id, self.platform, type_, id_, parent_id, payload)


def begin_turn(ctx: Any) -> Optional[TurnScope]:
    """``turn.start`` for a gateway turn (``TurnContext``); ``None`` when nobody listens."""
    session_key = getattr(ctx, "session_key", None)
    if not session_key or not enabled():
        return None
    scope = TurnScope(session_key=session_key, session_id=getattr(ctx, "session_id", None),
                      platform=_source_platform(getattr(ctx, "source", None)), turn_id=f"turn_{uuid.uuid4().hex}")
    inbound = getattr(ctx, "inbound_message_id", None)
    scope.emit("turn.start", scope.turn_id, None, {
        "message_id": None if inbound is None else str(inbound),
        "run_generation": getattr(ctx, "run_generation", None),
    })
    return scope


def end_turn(scope: Optional[TurnScope], *, result: Any = None, error: Optional[BaseException] = None) -> None:
    """Exactly one terminal event per turn: ``turn.done`` | ``turn.stopped`` | ``error``."""
    if scope is None:
        return
    try:
        if error is not None:
            scope.emit("error", scope.turn_id, None, {"stage": "turn", "error_type": type(error).__name__,
                                                     "message": _capped(str(error), 1024)[0]})
            return
        res = result if isinstance(result, dict) else {}
        payload = {
            "final_response": _redact(res.get("final_response") or ""),
            "completed": res.get("completed"), "api_calls": res.get("api_calls", 0),
            "model": res.get("model"), "session_id": res.get("session_id") or scope.session_id,
        }
        if res.get("session_id"):
            scope.session_id = res["session_id"]
        if res.get("interrupted"):
            payload["reason"] = _capped(res.get("interrupt_message") or "", 1024)[0]
            scope.emit("turn.stopped", scope.turn_id, None, payload)
        elif res.get("failed"):
            payload["failure_reason"] = res.get("failure_reason")
            payload["message"] = _capped(res.get("error") or "", 1024)[0]
            scope.emit("error", scope.turn_id, None, payload)
        else:
            scope.emit("turn.done", scope.turn_id, None, payload)
    except Exception:
        logger.debug("turn end emit failed", exc_info=True)


_SUBAGENT_TYPES = {"subagent.start": "subagent.start", "subagent.complete": "subagent.end"}


def wire_agent(scope: Optional[TurnScope], agent: Any) -> None:
    """Tee the agent's per-turn callbacks into the stream. Existing callbacks keep running first-class;
    a ``None`` stream/interim callback stays ``None`` (setting one would switch the agent into
    streaming mode and change delivery)."""
    if scope is None or agent is None:
        return

    def tee(name: str, observer: Callable[..., None], *, keep_none: bool = False) -> None:
        original = getattr(agent, name, None)
        if original is None and keep_none:
            return

        def wrapped(*args, **kwargs):
            try:
                observer(*args, **kwargs)
            except Exception:
                logger.debug("session event observer failed (%s)", name, exc_info=True)
            if original is not None:
                return original(*args, **kwargs)
            return None

        wrapped.__wrapped__ = original  # type: ignore[attr-defined]
        setattr(agent, name, wrapped)

    def on_tool_start(call_id, tool_name, args=None, *_a, **_k):
        call = str(call_id or "")
        scope.tool_names[call] = str(tool_name or "")
        text, truncated = _capped(args or {})
        scope.emit("tool.start", call, scope.turn_id, {"tool_name": str(tool_name or ""), "args": text,
                                                       "truncated": truncated})

    def on_tool_complete(call_id, tool_name, args=None, result=None, *_a, **_k):
        call = str(call_id or "")
        scope.tool_names.pop(call, None)
        text, truncated = _capped(result)
        is_error = False
        try:
            from agent.display import _detect_tool_failure
            is_error = bool(_detect_tool_failure(str(tool_name or ""), result)[0])
        except Exception:
            pass
        scope.emit("tool.end", call, scope.turn_id, {"tool_name": str(tool_name or ""), "output": text,
                                                     "truncated": truncated, "is_error": is_error})

    def on_progress(event_type, tool_name=None, preview=None, args=None, **kwargs):
        if event_type in _SUBAGENT_TYPES or event_type.startswith("subagent"):
            sub_id = kwargs.get("subagent_id")
            payload = {k: kwargs.get(k) for k in ("goal", "status", "depth", "model", "child_session_id",
                                                  "delegation_id", "tool_count", "duration_seconds")
                       if kwargs.get(k) is not None}
            if event_type not in _SUBAGENT_TYPES:
                payload["kind"] = event_type.replace("subagent_", "subagent.").split(".", 1)[-1]
                if tool_name:
                    payload["tool_name"] = str(tool_name)
            if preview:
                payload["preview"] = _capped(preview, 1024)[0]
            scope.emit(_SUBAGENT_TYPES.get(event_type, "subagent.progress"),
                       None if sub_id is None else str(sub_id),
                       str(kwargs.get("parent_id") or scope.turn_id), payload)
        elif event_type == "tool.output_risk":
            call = kwargs.get("tool_call_id")
            risk = kwargs.get("risk_metadata") or {}
            scope.emit("tool.progress", None if call is None else str(call), scope.turn_id,
                       {"kind": "output_risk", "tool_name": str(tool_name or ""), "risk": risk.get("risk")})

    def on_delta(text=None, *_a, **_k):
        if isinstance(text, str) and text:
            scope.emit("text.delta", None, scope.turn_id, {"text": text})

    def on_interim(text, *_a, already_streamed: bool = False, **_k):
        scope.emit("text.interim", None, scope.turn_id, {"text": str(text or ""),
                                                         "already_streamed": bool(already_streamed)})

    def on_status(event_type, message=None, *_a, **_k):
        scope.emit("status", None, scope.turn_id, {"kind": str(event_type), "message": _capped(message or "", 1024)[0]})

    tee("tool_start_callback", on_tool_start)
    tee("tool_complete_callback", on_tool_complete)
    tee("tool_progress_callback", on_progress)
    tee("stream_delta_callback", on_delta, keep_none=True)
    tee("interim_assistant_callback", on_interim, keep_none=True)
    tee("status_callback", on_status)


def is_delegated_child() -> bool:
    try:
        from agent.delegation_context import is_delegated_child_context
        return is_delegated_child_context()
    except Exception:
        return False

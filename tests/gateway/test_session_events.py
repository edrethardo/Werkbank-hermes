"""``on_gateway_session_event``: one ordered, gap-checked event stream per gateway session (WB-1126).

A plugin (the Werkbank chat bridge) mirrors a Telegram session live. These tests drive the real
gateway pieces — ``_handle_message`` admission, ``_run_agent`` with a fake agent, the busy
steer/queue funnels, the gateway approval wait and the clarify primitive — and read what a
subscriber registered exactly like ``ctx.register_hook`` receives.
"""

from __future__ import annotations

import importlib
import sys
import threading
import time
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway import session_events
from gateway.config import GatewayConfig, Platform, PlatformConfig, StreamingConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter, _make_runner

HOOK = "on_gateway_session_event"


@pytest.fixture(autouse=True)
def _clean_bus():
    session_events._reset_for_tests()
    yield
    session_events._reset_for_tests()


def _subscribe(callback=None):
    """Register a subscriber on the active home's plugin manager (what a loaded plugin does)."""
    from hermes_cli.plugins import get_plugin_manager

    seen: list[dict] = []

    def default(event, **_kw):
        seen.append(event)

    manager = get_plugin_manager()
    manager._discovered = True
    manager._hooks.setdefault(HOOK, []).append(callback or default)
    return seen


def _settle(seen, pred=None, timeout=5.0):
    assert session_events.wait_idle(timeout), "subscriber queue never drained"
    if pred is not None:
        deadline = time.monotonic() + timeout
        while not pred(seen) and time.monotonic() < deadline:
            time.sleep(0.01)
    return seen


def _of(seen, key=None):
    return [e for e in seen if key is None or e["session_key"] == key]


# ── hook name ───────────────────────────────────────────────────────────────────────────────


def test_hook_name_is_a_valid_plugin_hook():
    from hermes_cli.plugins import VALID_HOOKS

    assert HOOK in VALID_HOOKS
    assert session_events.HOOK == HOOK


# ── a full Telegram turn, in order, gap-free ────────────────────────────────────────────────


class ToolAndStreamAgent:
    """Fires the agent's real lifecycle callbacks the way tool_executor / stream_delivery do."""

    def __init__(self, **kwargs):
        self.tools = []
        self.tool_progress_callback = None
        self.tool_start_callback = None
        self.tool_complete_callback = None
        self.stream_delta_callback = None

    def run_conversation(self, message, conversation_history=None, task_id=None, **kwargs):
        noop = lambda *a, **k: None  # noqa: E731 — production skips a None callback the same way
        (self.tool_progress_callback or noop)("tool.started", "terminal", "pwd", {"command": "pwd"})
        (self.tool_start_callback or noop)("call-1", "terminal", {"command": "pwd"})
        (self.tool_complete_callback or noop)("call-1", "terminal", {"command": "pwd"}, "/home/x\n" + "y" * 9000)
        (self.tool_progress_callback or noop)("tool.completed", "terminal", None, None, duration=0.1, is_error=False)
        (self.stream_delta_callback or noop)("Hel")
        (self.stream_delta_callback or noop)("lo.")
        return {"final_response": "Hello.", "messages": [], "api_calls": 1}


def _turn_runner(monkeypatch, tmp_path):
    import hermes_yaml as yaml

    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "display": {"tool_progress": "all", "interim_assistant_messages": False},
        "streaming": {"enabled": True, "edit_interval": 0.01, "buffer_threshold": 1},
    }), encoding="utf-8")
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = ToolAndStreamAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    adapter = ProgressCaptureAdapter(platform=Platform.TELEGRAM)
    runner = _make_runner(adapter)
    runner.config.streaming = StreamingConfig.from_dict({"enabled": True, "edit_interval": 0.01, "buffer_threshold": 1})
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    return runner


def _admission_runner(turn_runner, source):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True)})
    adapter = SimpleNamespace(send=AsyncMock())
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.pairing_store = MagicMock()
    runner.session_store = MagicMock()
    runner._is_user_authorized_for_source = lambda _s: True
    runner._admit_bot_message_for_source = lambda _s: True
    runner._delivery_adapter_for = lambda _s: adapter
    runner._intake_adapter_for = lambda _s: adapter
    runner._rescue_orphaned_overflow = lambda _k, _a: None
    runner._enqueue_fifo = MagicMock()
    runner._run_post_turn_hooks = AsyncMock()

    async def handle_with_agent(event, src, quick_key, generation):
        return await turn_runner._run_agent(
            message=event.text, context_prompt="", history=[], source=source,
            session_id="sess-telegram-1", session_key=quick_key,
        )

    runner._handle_message_with_agent = handle_with_agent
    return runner


@pytest.mark.asyncio
async def test_telegram_turn_streams_in_order_with_gapless_core_seq(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "*")
    seen = _subscribe()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="4711", chat_type="dm", user_id="4711",
                           user_name="owner", thread_id="51")
    runner = _admission_runner(_turn_runner(monkeypatch, tmp_path), source)
    event = MessageEvent(text="what is here?", message_id="tg-100", source=source,
                         metadata={"client_message_id": "cm-1"})

    await runner._handle_message(event)
    _settle(seen, lambda s: any(e["type"] == "turn.done" for e in s))

    keys = {e["session_key"] for e in seen}
    assert len(keys) == 1, keys
    types_ = [e["type"] for e in seen]
    # text.delta… : one or more deltas, nothing else in between.
    collapsed = [t for i, t in enumerate(types_) if not (t == "text.delta" and i and types_[i - 1] == "text.delta")]
    assert collapsed == ["user.message", "turn.start", "tool.start", "tool.end", "text.delta", "turn.done"], types_
    assert [e["core_seq"] for e in seen] == list(range(1, len(seen) + 1))

    user, start, tool_start, tool_end = seen[0], seen[1], seen[2], seen[3]
    assert user["payload"]["delivery"] == "turn"
    assert user["payload"]["client_message_id"] == "cm-1"
    assert user["payload"]["text"] == "what is here?"
    assert user["origin"] == "telegram" and user["platform"] == "telegram"
    assert start["id"].startswith("turn_") and start["session_id"] == "sess-telegram-1"
    assert tool_start["id"] == tool_end["id"] == "call-1"
    assert tool_start["parent_id"] == tool_end["parent_id"] == start["id"]
    assert tool_end["payload"]["truncated"] is True
    assert len(tool_end["payload"]["output"].encode()) <= 4096
    assert "".join(e["payload"]["text"] for e in seen if e["type"] == "text.delta") == "Hello."
    done = seen[-1]
    assert done["parent_id"] is None and done["id"] == start["id"]
    assert done["payload"]["final_response"] == "Hello."
    assert all(e["v"] == 1 and e["ts"] for e in seen)


# ── follow-ups during a running turn ────────────────────────────────────────────────────────


class _Receiver:
    _supports_active_turn_redirect = True

    def __init__(self):
        self.payloads = []

    def steer(self, text):
        self.payloads.append(text)
        return True

    redirect = steer


def _tg_event(text, mid, **meta):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="4711", chat_type="dm", user_id="4711", thread_id="51")
    return MessageEvent(text=text, message_id=mid, source=source, metadata=dict(meta))


@pytest.mark.asyncio
async def test_steered_telegram_message_is_user_message_nachgeschoben(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    seen = _subscribe()
    runner = GatewayRunner(config=GatewayConfig())
    receiver = _Receiver()
    runner._session_state("key").turn.agent = receiver

    await runner._resolve_busy_steer_or_redirect(_tg_event("also check logs", "tg-2", client_message_id="cm-2"),
                                                 "key", "steer", receiver)
    await runner._busy_steer_command(_tg_event("/steer and the db", "tg-3"), "key", None)
    _settle(seen, lambda s: len(s) >= 2)

    assert [(e["type"], e["payload"]["delivery"], e["id"]) for e in seen] == [
        # The event id is the sender's own id when it has one (dedupe key), else the platform id.
        ("user.message", "nachgeschoben", "cm-2"), ("user.message", "nachgeschoben", "tg-3"),
    ]
    assert seen[0]["payload"]["client_message_id"] == "cm-2" and seen[0]["payload"]["message_id"] == "tg-2"
    assert seen[1]["payload"]["text"] == "and the db"
    assert [e["core_seq"] for e in seen] == [1, 2]


def test_queued_telegram_message_is_user_message_eingereiht(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    seen = _subscribe()
    runner = GatewayRunner(config=GatewayConfig())
    adapter = SimpleNamespace(_pending_messages={})
    runner._delivery_adapter_for = lambda _s: adapter

    runner._queue_or_replace_pending_event("key", _tg_event("later please", "tg-9"))
    _settle(seen, lambda s: len(s) >= 1)

    assert [(e["type"], e["payload"]["delivery"], e["id"]) for e in seen] == [("user.message", "eingereiht", "tg-9")]


def test_plugin_injected_message_carries_its_origin(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    seen = _subscribe()
    runner = GatewayRunner(config=GatewayConfig())
    adapter = SimpleNamespace(_pending_messages={})
    runner._delivery_adapter_for = lambda _s: adapter
    event = _tg_event("from the board", None, hermes_plugin_id="werkbank", hermes_plugin_injection=True,
                      gateway_session_id="sess-9", client_message_id="wb-77")
    event.internal = True

    runner._queue_or_replace_pending_event("key", event)
    _settle(seen, lambda s: len(s) >= 1)

    assert seen[0]["origin"] == "werkbank"
    assert seen[0]["id"] == "wb-77" and seen[0]["session_id"] == "sess-9"


# ── every enqueue path announces the message exactly once (review WB-1126) ───────────────


def _busy_runner(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner = GatewayRunner(config=GatewayConfig())
    adapter = SimpleNamespace(_pending_messages={})
    runner._delivery_adapter_for = lambda _s: adapter
    return runner, adapter


def _announced(seen):
    return [(e["payload"]["delivery"], e["id"], e["payload"]["text"]) for e in seen if e["type"] == "user.message"]


@pytest.mark.parametrize("mode", ["interrupt", "queue"])
def test_telegram_grace_window_followups_each_announced_once(tmp_path, monkeypatch, mode):
    """Merge decision: every merged message keeps its OWN user.message; a message folded into an
    already queued one names that head in ``merged_into`` (both run as one turn)."""
    runner, adapter = _busy_runner(tmp_path, monkeypatch)
    seen = _subscribe()
    runner._session_state("key").turn.started_ts = time.time()
    first, second = _tg_event("one", "tg-a", client_message_id="cm-a"), _tg_event("two", "tg-b")

    assert runner._hm_busy_telegram_grace_queue(first, first.source, "key", mode)
    assert runner._hm_busy_telegram_grace_queue(second, second.source, "key", mode)
    _settle(seen, lambda s: len(s) >= 2)

    assert _announced(seen) == [("eingereiht", "cm-a", "one"), ("eingereiht", "tg-b", "two")]
    merged = [e["payload"].get("merged_into") for e in seen]
    assert merged == ([None, "cm-a"] if mode == "interrupt" else [None, None])


def test_photo_followup_is_announced(tmp_path, monkeypatch):
    import asyncio

    runner, adapter = _busy_runner(tmp_path, monkeypatch)
    seen = _subscribe()
    photo = _tg_event("look", "tg-p")
    photo.message_type = MessageType.PHOTO
    photo.media_urls, photo.media_types = ["/tmp/p.jpg"], ["image/jpeg"]

    handled, _ = asyncio.run(runner._hm_busy_slash_or_photo(photo, photo.source, "key"))
    assert handled
    _settle(seen, lambda s: len(s) >= 1)
    assert _announced(seen) == [("eingereiht", "tg-p", "look")]
    assert seen[0]["payload"]["media_count"] == 1


@pytest.mark.asyncio
async def test_message_while_agent_still_starting_is_announced(tmp_path, monkeypatch):
    from gateway.run import _AGENT_PENDING_SENTINEL

    runner, adapter = _busy_runner(tmp_path, monkeypatch)
    seen = _subscribe()
    runner._session_state("key").turn.agent = _AGENT_PENDING_SENTINEL
    event = _tg_event("hurry", "tg-s")

    assert await runner._hm_handle_running_session_message(event, event.source, "key") is None
    assert adapter._pending_messages["key"] is event
    _settle(seen, lambda s: len(s) >= 1)
    assert _announced(seen) == [("eingereiht", "tg-s", "hurry")]


@pytest.mark.asyncio
async def test_queue_command_announces_the_queued_text_with_the_senders_id(tmp_path, monkeypatch):
    runner, adapter = _busy_runner(tmp_path, monkeypatch)
    seen = _subscribe()
    event = _tg_event("/queue after that, tests", "tg-q", client_message_id="cm-q")

    await runner._busy_queue_command(event, "key", event.source)
    _settle(seen, lambda s: len(s) >= 1)
    assert _announced(seen) == [("eingereiht", "cm-q", "after that, tests")]
    assert seen[0]["payload"]["message_id"] == "tg-q"


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["starting", "none"])
async def test_steer_queue_fallback_is_announced(tmp_path, monkeypatch, agent):
    from gateway.run import _AGENT_PENDING_SENTINEL

    runner, adapter = _busy_runner(tmp_path, monkeypatch)
    seen = _subscribe()
    if agent == "starting":
        runner._session_state("key").turn.agent = _AGENT_PENDING_SENTINEL
    event = _tg_event("/steer also the db", "tg-st")

    await runner._busy_steer_command(event, "key", event.source)
    _settle(seen, lambda s: len(s) >= 1)
    assert _announced(seen) == [("eingereiht", "tg-st", "also the db")]


@pytest.mark.asyncio
async def test_media_interrupt_without_redirect_is_announced(tmp_path, monkeypatch):
    runner, adapter = _busy_runner(tmp_path, monkeypatch)
    seen = _subscribe()
    event = _tg_event("read this", "tg-doc")
    event.message_type = MessageType.DOCUMENT
    event.media_urls, event.media_types = ["/tmp/a.pdf"], ["application/pdf"]
    agent = SimpleNamespace(interrupt=MagicMock())

    await runner._hm_busy_interrupt(event, event.source, agent, "key")
    agent.interrupt.assert_called_once()
    _settle(seen, lambda s: len(s) >= 1)
    assert _announced(seen) == [("eingereiht", "tg-doc", "read this")]
    assert seen[0]["payload"]["interrupt"] is True


@pytest.mark.asyncio
async def test_queue_mode_text_left_to_the_adapter_is_announced(tmp_path, monkeypatch):
    runner, adapter = _busy_runner(tmp_path, monkeypatch)
    seen = _subscribe()
    runner._is_user_authorized_for_source = lambda _s: True
    runner._admit_bot_message_for_source = lambda _s: True
    runner._route_plaintext_approval_while_busy = AsyncMock(return_value=False)
    runner._effective_busy_input_mode = lambda _s: "queue"
    runner._effective_busy_text_mode = lambda _s: "queue"
    event = _tg_event("and then", "tg-qm")

    assert await runner._handle_active_session_busy_message(event, "key") is False
    _settle(seen, lambda s: len(s) >= 1)
    assert _announced(seen) == [("eingereiht", "tg-qm", "and then")]


@pytest.mark.parametrize("kind", ["text", "photo"])
def test_queue_or_replace_announces_exactly_once(tmp_path, monkeypatch, kind):
    runner, adapter = _busy_runner(tmp_path, monkeypatch)
    seen = _subscribe()
    head = _tg_event("first", "tg-1")
    adapter._pending_messages["key"] = head
    event = _tg_event("second", "tg-2")
    if kind == "photo":
        event.message_type = MessageType.PHOTO
        event.media_urls, event.media_types = ["/tmp/p.jpg"], ["image/jpeg"]

    runner._queue_or_replace_pending_event("key", event)
    session_events.emit("key", None, "telegram", "status", None, None, {})  # barrier
    _settle(seen, lambda s: len(s) >= 2)
    assert _announced(seen) == [("eingereiht", "tg-2", "second")]
    assert seen[0]["payload"].get("merged_into") == ("tg-1" if kind == "photo" else None)


def test_a_message_is_announced_once_even_when_it_later_runs_as_a_turn():
    seen = _subscribe()
    event = _tg_event("orphan", "tg-o")
    session_events.user_message("key", event, "eingereiht")
    session_events.user_message("key", event, "turn")  # rescued orphan admitted later
    session_events.emit("key", None, "telegram", "status", None, None, {})  # barrier
    _settle(seen, lambda s: len(s) >= 2)
    assert _announced(seen) == [("eingereiht", "tg-o", "orphan")]


def test_enqueue_without_announce_stays_silent(tmp_path, monkeypatch):
    """The leftover /steer re-staged by the drain was already announced as ``nachgeschoben``."""
    runner, adapter = _busy_runner(tmp_path, monkeypatch)
    seen = _subscribe()
    runner._enqueue_fifo("key", _tg_event("left over", None), adapter, announce=False)
    session_events.emit("key", None, "telegram", "status", None, None, {})  # barrier
    _settle(seen, lambda s: len(s) >= 1)
    assert _announced(seen) == []


@pytest.mark.asyncio
async def test_leftover_steer_restaged_by_the_drain_is_not_announced_again(tmp_path, monkeypatch):
    runner, adapter = _busy_runner(tmp_path, monkeypatch)
    seen = _subscribe()
    source = _tg_event("x", None).source
    adapter.get_pending_message = lambda key: adapter._pending_messages.pop(key, None)
    result = {"interrupted": True, "interrupt_message": "new direction", "pending_steer": "and the db"}

    await runner._run_agent_drain_pending(result, adapter, source, "key")
    assert getattr(adapter._pending_messages.get("key"), "text", None) == "and the db"
    session_events.emit("key", None, "telegram", "status", None, None, {})  # barrier
    _settle(seen, lambda s: len(s) >= 1)
    assert _announced(seen) == []


# ── approvals: exact request id, decided_by, redaction ──────────────────────────────────────


def _wait_decision(session_key, data, answer):
    from tools import approval
    from tools.approval_gateway_wait import _await_gateway_decision

    box = {}

    def notify(payload):
        box["request"] = payload
        if answer is not None:
            threading.Timer(0.05, answer, args=(payload,)).start()

    approval.register_gateway_notify(session_key, notify)
    try:
        box["decision"] = _await_gateway_decision(session_key, notify, dict(data))
    finally:
        approval.unregister_gateway_notify(session_key)
    return box


def test_approval_request_carries_the_resolvable_request_id_and_resolved_carries_decided_by():
    from tools.approval import resolve_gateway_approval

    seen = _subscribe()
    counts = []
    box = _wait_decision("agent:main:telegram:dm:4711:51", {"command": "rm -rf build", "pattern_key": "rm"},
                         lambda req: counts.append(resolve_gateway_approval(
                             "agent:main:telegram:dm:4711:51", "once", request_id=req["request_id"],
                             decided_by="werkbank")))
    _settle(seen, lambda s: len(s) >= 2)

    assert counts == [1] and box["decision"]["choice"] == "once"
    request, resolved = seen[0], seen[1]
    assert request["type"] == "approval.request"
    assert request["payload"]["request_id"] == box["request"]["request_id"]
    assert request["id"] == box["request"]["request_id"]
    assert resolved["type"] == "approval.resolved" and resolved["id"] == request["id"]
    assert resolved["payload"] == {"request_id": request["id"], "choice": "once", "decided_by": "werkbank"}


def test_resolve_without_decided_by_keeps_old_callers_working():
    from tools.approval import resolve_gateway_approval

    seen = _subscribe()
    _wait_decision("k-old", {"command": "ls", "pattern_key": "x"},
                   lambda req: resolve_gateway_approval("k-old", "deny"))
    _settle(seen, lambda s: len(s) >= 2)
    assert seen[1]["payload"]["choice"] == "deny" and seen[1]["payload"]["decided_by"] is None


def test_approval_timeout_is_expired(monkeypatch):
    from tools import approval_context

    monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 0.05)
    seen = _subscribe()
    _wait_decision("k-exp", {"command": "ls", "pattern_key": "x"}, None)
    _settle(seen, lambda s: len(s) >= 2)
    assert [e["type"] for e in seen] == ["approval.request", "approval.expired"]


def test_approval_command_is_redacted_like_the_telegram_card():
    from gateway.run import _redact_approval_command

    seen = _subscribe()
    secret_cmd = "curl -H 'Authorization: Bearer sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789' https://x"
    _wait_decision("k-red", {"command": secret_cmd, "pattern_key": "curl"},
                   lambda req: __import__("tools.approval", fromlist=["x"]).resolve_gateway_approval("k-red", "deny"))
    _settle(seen, lambda s: len(s) >= 2)
    shown = seen[0]["payload"]["command"]
    assert shown == _redact_approval_command(secret_cmd)
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in shown


@pytest.mark.asyncio
async def test_telegram_button_click_reports_decided_by_telegram(monkeypatch):
    from tools import approval

    calls = []
    monkeypatch.setattr(approval, "resolve_gateway_approval", lambda *a, **k: calls.append(k) or 1)
    from tests.gateway.test_telegram_approval_request_binding import adapter as tg_adapter, query

    a = tg_adapter()
    await a.send_exec_approval("12345", "op", "s", metadata={"approval_request_id": "R", "thread_id": "51"})
    token = next(iter(a._approval_state))
    await a._handle_exec_approval_callback(query(), "ea:once:" + token, {"chat_id": "12345", "thread_id": "51"})
    assert calls and calls[0].get("decided_by") == "telegram" and calls[0].get("request_id") == "R"


# ── clarify ─────────────────────────────────────────────────────────────────────────────────


def test_clarify_request_and_resolution_carry_clarify_id():
    from tools import clarify_gateway as cg

    seen = _subscribe()
    cg.register("cl-1", "k-cl", "Which db?", ["pg", "sqlite"])
    threading.Timer(0.05, cg.resolve_gateway_clarify, args=("cl-1", "pg")).start()
    assert cg.wait_for_response("cl-1", timeout=5) == "pg"
    cg.register("cl-2", "k-cl", "Name?", None)
    assert cg.wait_for_response("cl-2", timeout=0.05) is None
    _settle(seen, lambda s: len(s) >= 4)

    assert [(e["type"], e["id"]) for e in seen] == [
        ("clarify.request", "cl-1"), ("clarify.resolved", "cl-1"),
        ("clarify.request", "cl-2"), ("clarify.expired", "cl-2"),
    ]
    assert seen[0]["payload"]["choices"] == ["pg", "sqlite"] and seen[1]["payload"]["answer"] == "pg"


# ── backpressure: never a silent loss ───────────────────────────────────────────────────────


def test_slow_subscriber_gets_overflow_instead_of_a_silent_gap(monkeypatch):
    monkeypatch.setattr(session_events, "QUEUE_CAP", 3)
    entered, release = threading.Event(), threading.Event()
    got: list[dict] = []

    def slow(event, **_kw):
        got.append(event)
        entered.set()
        release.wait(5)

    _subscribe(slow)
    session_events.emit("k-slow", "s", "telegram", "text.delta", None, None, {"text": "0"})
    assert entered.wait(5)
    for i in range(1, 10):
        session_events.emit("k-slow", "s", "telegram", "text.delta", None, None, {"text": str(i)})
    release.set()
    _settle(got, lambda g: any(e["type"] == "overflow" for e in g))

    delivered = [e["core_seq"] for e in got if e["type"] != "overflow"]
    overflow = [e for e in got if e["type"] == "overflow"]
    assert len(overflow) == 1 and overflow[0]["session_key"] == "k-slow"
    lost = range(overflow[0]["payload"]["first_seq"], overflow[0]["payload"]["last_seq"] + 1)
    assert overflow[0]["payload"]["dropped"] == len(lost)
    assert sorted(delivered + list(lost)) == list(range(1, 11))
    assert got.index(overflow[0]) == len(delivered)  # reported before anything after the gap


def test_sequence_is_per_session_key():
    seen = _subscribe()
    for key in ("a", "b", "a", "a", "b"):
        session_events.emit(key, None, "telegram", "status", None, None, {})
    _settle(seen, lambda s: len(s) >= 5)
    assert [e["core_seq"] for e in _of(seen, "a")] == [1, 2, 3]
    assert [e["core_seq"] for e in _of(seen, "b")] == [1, 2]


def test_failing_subscriber_does_not_block_the_others():
    def broken(**_kw):
        raise RuntimeError("plugin bug")

    _subscribe(broken)
    seen = _subscribe()
    session_events.emit("k", None, "telegram", "status", None, None, {})
    _settle(seen, lambda s: len(s) >= 1)
    assert len(seen) == 1


# ── no subscriber: no work ──────────────────────────────────────────────────────────────────


def test_without_subscriber_nothing_is_built_or_wired(monkeypatch):
    from hermes_cli import plugins

    lookups = []
    monkeypatch.setattr(plugins, "iter_hook_callbacks", lambda name: lookups.append(name) or ())
    assert session_events.emit("k", None, "telegram", "status", None, None, {"x": 1}) is None
    assert lookups == [], "callback lookup must not happen without a subscriber (has_hook short circuit)"
    assert session_events._sessions == {} and session_events._subscribers == {}
    assert session_events.begin_turn(SimpleNamespace(session_key="k", session_id="s", source=None)) is None

    agent = SimpleNamespace(tool_progress_callback=object(), tool_start_callback=None, tool_complete_callback=None,
                            stream_delta_callback=object(), interim_assistant_callback=None, status_callback=object())
    before = dict(vars(agent))
    session_events.wire_agent(None, agent)
    assert vars(agent) == before
    assert not any(t.name.startswith("gateway-session-events") for t in threading.enumerate())


# ── subscriber worker robustness (review WB-1126) ──────────────────────────────────────────


def test_subscriber_raising_systemexit_keeps_its_worker():
    calls = []

    def exits(event, **_kw):
        calls.append(event["core_seq"])
        if len(calls) == 1:
            raise SystemExit(3)

    _subscribe(exits)
    session_events.emit("k", None, "telegram", "status", None, None, {})
    assert session_events.wait_idle(5)
    worker = next(iter(session_events._subscribers.values())).thread
    session_events.emit("k", None, "telegram", "status", None, None, {})
    assert session_events.wait_idle(5)
    assert calls == [1, 2]
    assert worker.is_alive() and next(iter(session_events._subscribers.values())).thread is worker


def test_dead_worker_is_restarted_without_losing_its_queue(monkeypatch):
    monkeypatch.setattr(threading, "excepthook", lambda _args: None)
    entered, release = threading.Event(), threading.Event()
    got = []

    def dies_once(event, **_kw):
        got.append(event["core_seq"])
        if len(got) == 1:
            entered.set()
            release.wait(5)
            raise KeyboardInterrupt  # not swallowed: the worker thread dies with 2 events queued

    _subscribe(dies_once)
    session_events.emit("k", None, "telegram", "status", None, None, {})
    assert entered.wait(5)
    session_events.emit("k", None, "telegram", "status", None, None, {})
    session_events.emit("k", None, "telegram", "status", None, None, {})
    old = next(iter(session_events._subscribers.values())).thread
    release.set()
    old.join(5)
    assert not old.is_alive()
    session_events.emit("k", None, "telegram", "status", None, None, {})
    deadline = time.monotonic() + 5
    while len(got) < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert got == [1, 2, 3, 4]


def test_worker_start_failure_is_not_a_silent_gap(monkeypatch):
    real = session_events._Subscriber._thread_factory
    fail = {"on": True}

    def factory(*a, **kw):
        if fail["on"]:
            raise RuntimeError("can't start new thread")
        return real(*a, **kw)

    monkeypatch.setattr(session_events._Subscriber, "_thread_factory", staticmethod(factory))
    seen = _subscribe()
    assert session_events.emit("k", None, "telegram", "status", None, None, {}) == 1
    assert session_events.emit("k", None, "telegram", "status", None, None, {}) == 2
    fail["on"] = False
    assert session_events.emit("k", None, "telegram", "status", None, None, {}) == 3
    _settle(seen, lambda s: len(s) >= 3)
    assert [e["core_seq"] for e in seen] == [1, 2, 3]


def test_offer_failure_becomes_overflow_not_a_gap(monkeypatch):
    seen = _subscribe()
    session_events.emit("k", None, "telegram", "status", None, None, {})
    _settle(seen, lambda s: len(s) >= 1)
    sub = next(iter(session_events._subscribers.values()))
    real_offer = sub.offer
    monkeypatch.setattr(sub, "offer", lambda *_a: (_ for _ in ()).throw(RuntimeError("boom")))
    session_events.emit("k", None, "telegram", "status", None, None, {})
    monkeypatch.setattr(sub, "offer", real_offer)
    session_events.emit("k", None, "telegram", "status", None, None, {})
    _settle(seen, lambda s: len(s) >= 3)
    assert [(e["type"], e["core_seq"]) for e in seen] == [("status", 1), ("overflow", None), ("status", 3)]
    assert seen[1]["payload"]["first_seq"] == seen[1]["payload"]["last_seq"] == 2


def test_capped_redacts_only_a_bounded_prefix(monkeypatch):
    lengths = []
    monkeypatch.setattr(session_events, "_redact", lambda text: lengths.append(len(text)) or text)
    text, truncated = session_events._capped("a" * 1_000_000, 100)
    assert truncated and len(text.encode()) <= 100
    assert lengths and max(lengths) <= 200


def test_capped_never_leaks_a_secret_cut_at_the_prefix_boundary():
    # Two full secrets shrink to ~14 chars each when redacted, which pulls the text that sits at
    # the 2x-limit pre-cut boundary into the visible window. The secret cut there is only a
    # fragment ("sk-proj-Z9Z") that the redactor no longer recognises: it must not be shown.
    secret = "sk-proj-" + "Z9" * 40
    raw = (secret + " ") * 2 + "y" * 10 + " " + secret
    assert raw.index(secret, 180) < 200 < raw.index(secret, 180) + len(secret)
    text, truncated = session_events._capped(raw, 100)
    assert truncated and "sk-proj-" not in text

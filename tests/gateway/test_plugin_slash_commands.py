"""Slash commands from a plugin (the Werkbank board chat) — WB-1256.

The board's ``/status``, ``/model``, ``/new``, ``/stop`` take the same gateway command path a
Telegram message of the session's owner takes; the reply and any picker go to the session's event
stream (``command.reply``, ``choice.request``), never to the model and never to the chat.
"""

from __future__ import annotations

import asyncio
import threading
import time
import types
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway import plugin_commands, session_events
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource, build_session_key

HOOK = "on_gateway_session_event"
PLUGIN = "platforms/werkbank"


@pytest.fixture(autouse=True)
def _clean():
    session_events._reset_for_tests()
    plugin_commands._reset_for_tests()
    yield
    session_events._reset_for_tests()
    plugin_commands._reset_for_tests()


def _subscribe():
    from hermes_cli.plugins import get_plugin_manager

    seen: list = []
    manager = get_plugin_manager()
    manager._discovered = True
    manager._hooks.setdefault(HOOK, []).append(lambda event=None, **_kw: seen.append(event))
    return seen


def _settle(seen, pred, timeout=5.0):
    assert session_events.wait_idle(timeout)
    deadline = time.monotonic() + timeout
    while not pred(seen) and time.monotonic() < deadline:
        time.sleep(0.01)
    return seen


def _of(seen, type_):
    return [e for e in seen if e["type"] == type_]


def _source():
    return SessionSource(platform=Platform.TELEGRAM, user_id="u1", chat_id="c1", user_name="owner",
                         chat_type="dm")


KEY = build_session_key(_source())


def _command_event(text, cmid="wb-1"):
    """What ``_dispatch_plugin_message_injection(as_command=True)`` hands the adapter."""
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=_source(), internal=True,
                        allow_gateway_control=True,
                        metadata={"hermes_plugin_id": PLUGIN, "hermes_plugin_injection": True,
                                  "gateway_session_key": KEY, "gateway_session_id": "sess-1",
                                  "gateway_session_strict": True, "client_message_id": cmid,
                                  plugin_commands.COMMAND_FLAG: True})


def _entry():
    now = datetime.now()
    return SessionEntry(session_key=KEY, session_id="sess-1", created_at=now, updated_at=now,
                        origin=_source(), platform=Platform.TELEGRAM, chat_type="dm")


def _status_runner():
    from tests.gateway.test_status_command import _make_runner
    runner = _make_runner(_entry())
    runner._handle_message_with_agent = AsyncMock(side_effect=AssertionError("command reached the model"))
    return runner


# ── the event: a command, and only for the plugin that asked ─────────────────────────────────


def test_only_an_injected_command_event_counts():
    assert plugin_commands.is_plugin_command(_command_event("/status"))
    human = MessageEvent(text="/status", source=_source(),
                         metadata={"hermes_plugin_injection": True, plugin_commands.COMMAND_FLAG: True})
    assert not plugin_commands.is_plugin_command(human)          # not internal: a chat message
    plain = _command_event("/status")
    plain.metadata.pop(plugin_commands.COMMAND_FLAG)
    assert not plugin_commands.is_plugin_command(plain)          # an ordinary board message


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.TELEGRAM)
        self.sent = []

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return SendResult(success=True, message_id="9")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "dm"}


def _inject_runner(adapter, *, authorized=True):
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.session_store = SimpleNamespace()
    runner._async_session_store = SimpleNamespace(_store=runner.session_store,
                                                  lookup_by_session_key=AsyncMock(return_value=_entry()))
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._profile_adapters = {}
    runner._running = True
    runner._draining = False
    runner._background_tasks = set()
    runner._is_user_authorized = MagicMock(return_value=authorized)
    return runner


@pytest.mark.asyncio
async def test_as_command_takes_the_command_path_plain_text_does_not():
    adapter = _Adapter()
    seen_events = []
    adapter.handle_message = AsyncMock(side_effect=lambda e: seen_events.append(e))
    runner = _inject_runner(adapter)

    assert await runner._dispatch_plugin_message_injection(
        session_key=KEY, content="/status", plugin_id=PLUGIN, client_message_id="wb-1", as_command=True)
    assert await runner._dispatch_plugin_message_injection(
        session_key=KEY, content="/status", plugin_id=PLUGIN, client_message_id="wb-2")

    command, text = seen_events
    assert command.is_command() and command.get_command() == "status"
    assert plugin_commands.is_plugin_command(command)
    assert not text.is_command()                       # unchanged: board text stays conversation
    assert not plugin_commands.is_plugin_command(text)


@pytest.mark.asyncio
async def test_unauthorized_session_refuses_the_command():
    seen = _subscribe()
    adapter = _Adapter()
    adapter.handle_message = AsyncMock()
    runner = _inject_runner(adapter, authorized=False)

    ok = await runner._dispatch_plugin_message_injection(
        session_key=KEY, content="/model", plugin_id=PLUGIN, client_message_id="wb-1", as_command=True)

    assert ok is False
    adapter.handle_message.assert_not_called()
    replies = _of(_settle(seen, lambda s: _of(s, "command.reply")), "command.reply")
    assert replies and replies[0]["payload"]["refused"] is True
    assert adapter.sent == []


# ── the reply goes to the stream, never to the model or the chat ─────────────────────────────


@pytest.mark.asyncio
async def test_status_reply_is_a_stream_event_not_a_chat_message():
    seen = _subscribe()
    runner = _status_runner()

    result = await runner._handle_message(_command_event("/status", cmid="wb-status"))

    assert result is None                                       # nothing for the chat
    reply = _of(_settle(seen, lambda s: _of(s, "command.reply")), "command.reply")[0]
    assert reply["session_key"] == KEY and reply["id"] == "wb-status"
    assert reply["payload"]["command"] == "status" and reply["payload"]["request"] == "/status"
    assert "tokens" in reply["payload"]["text"].lower()
    runner._handle_message_with_agent.assert_not_called()
    runner.adapters[Platform.TELEGRAM].send.assert_not_called()


@pytest.mark.asyncio
async def test_ist_befund_board_text_without_as_command_reaches_the_model():
    """What a board "/status" was before WB-1256 (and stays for a plugin without ``as_command``):
    ``allow_gateway_control=False`` makes ``is_command()`` False — the text runs as an agent turn."""
    runner = _status_runner()
    runner._handle_message_with_agent = AsyncMock(return_value="(Modellantwort)")
    runner._run_post_turn_hooks = AsyncMock()
    runner._claim_active_session_slot = lambda *_a: (None, None)
    runner._persist_active_agents = lambda: None
    event = _command_event("/status", cmid="wb-alt")
    event.allow_gateway_control = False
    event.metadata.pop(plugin_commands.COMMAND_FLAG)
    assert not event.is_command()
    await runner._handle_message(event)
    runner._handle_message_with_agent.assert_awaited_once()
    assert runner._handle_message_with_agent.await_args.args[0].text == "/status"


@pytest.mark.asyncio
async def test_telegram_status_still_returns_its_reply():
    runner = _status_runner()
    event = MessageEvent(text="/status", source=_source(), message_id="m1")
    assert "tokens" in (await runner._handle_message(event)).lower()


@pytest.mark.asyncio
async def test_unknown_command_gets_a_clear_notice_and_never_reaches_the_model(monkeypatch):
    seen = _subscribe()
    runner = _status_runner()
    monkeypatch.setattr("gateway.run._check_unavailable_skill", lambda _c: None)
    monkeypatch.setattr(runner, "_hm_skill_slash_rewrite",
                        lambda event, source, key, command: runner._hm_unknown_slash_reply(command, source),
                        raising=False)

    assert await runner._handle_message(_command_event("/gibtsnicht", cmid="wb-x")) is None

    reply = _of(_settle(seen, lambda s: _of(s, "command.reply")), "command.reply")[0]
    assert "/gibtsnicht" in reply["payload"]["text"]
    runner._handle_message_with_agent.assert_not_called()


@pytest.mark.asyncio
async def test_new_runs_the_reset_handler_and_reports_to_the_stream(monkeypatch):
    seen = _subscribe()
    runner = _status_runner()
    runner._handle_reset_command = AsyncMock(return_value="✨ Neue Sitzung.")
    async def confirm_off(**kw):          # approvals.destructive_slash_confirm: false
        return await kw["execute"]()

    monkeypatch.setattr(runner, "_maybe_confirm_destructive_slash", confirm_off, raising=False)
    monkeypatch.setattr(runner, "_is_telegram_topic_root_lobby", lambda _s: False, raising=False)

    assert await runner._handle_message(_command_event("/new", cmid="wb-new")) is None

    runner._handle_reset_command.assert_awaited_once()
    reply = _of(_settle(seen, lambda s: _of(s, "command.reply")), "command.reply")[0]
    assert reply["payload"]["text"] == "✨ Neue Sitzung." and reply["payload"]["command"] == "new"
    runner._handle_message_with_agent.assert_not_called()


@pytest.mark.asyncio
async def test_stop_while_a_turn_runs_interrupts_and_reports(monkeypatch):
    seen = _subscribe()
    runner = _status_runner()
    runner._interrupt_and_clear_session = AsyncMock()
    monkeypatch.setattr(runner, "_is_session_running", lambda _k: True, raising=False)
    monkeypatch.setattr(runner, "_hm_evict_idle_stale_agent", lambda _k: None, raising=False)
    monkeypatch.setattr(runner, "_hm_evict_reaped_agent", lambda _k: None, raising=False)

    assert await runner._handle_message(_command_event("/stop", cmid="wb-stop")) is None

    runner._interrupt_and_clear_session.assert_awaited_once()
    reply = _of(_settle(seen, lambda s: _of(s, "command.reply")), "command.reply")[0]
    assert reply["payload"]["command"] == "stop" and reply["payload"]["text"]
    runner._handle_message_with_agent.assert_not_called()


@pytest.mark.asyncio
async def test_board_stop_through_the_adapter_guard_sends_nothing_to_the_chat():
    """A running session: the adapter's bypass path calls the handler inline; the reply stays off
    the chat (the handler returns None for a plugin command)."""
    adapter = _Adapter()
    calls = []

    async def handler(event):
        calls.append(event)
        return await plugin_commands.handle(SimpleNamespace(), event, AsyncMock(return_value="⏹ gestoppt"))

    adapter.set_message_handler(handler)
    adapter._active_sessions[KEY] = asyncio.Event()
    adapter.cancel_session_processing = AsyncMock()
    await adapter.handle_message(_command_event("/stop", cmid="wb-stop"))
    assert calls and adapter.sent == []


# ── pickers become cards bound to their session ──────────────────────────────────────────────


def _model_runner(adapter):
    from tests.gateway.test_model_picker_persist import _make_runner
    return _make_runner(adapter)


def _answer_in_thread(*args, **kwargs):
    out = {}
    worker = threading.Thread(target=lambda: out.setdefault("r", plugin_commands.answer(*args, **kwargs)))
    return worker, out


async def _answer(*args, **kwargs):
    worker, out = _answer_in_thread(*args, **kwargs)
    worker.start()
    while worker.is_alive():
        await asyncio.sleep(0.01)
    return out["r"]


@pytest.mark.asyncio
async def test_model_picker_becomes_a_card_and_the_pick_switches_the_model(tmp_path, monkeypatch):
    from tests.gateway.test_model_picker_persist import _FakePickerAdapter, _setup_isolated_home
    seen = _subscribe()
    telegram = _FakePickerAdapter()
    _setup_isolated_home(tmp_path, monkeypatch, {"default": "old-model", "provider": "custom"})
    runner = _model_runner(telegram)

    async def inner(event):
        return await runner._handle_model_command(event)

    assert await plugin_commands.handle(runner, _command_event("/model", cmid="wb-m"), inner) is None
    assert telegram.captured_callback is None                   # nothing went to Telegram

    request = _of(_settle(seen, lambda s: _of(s, "choice.request")), "choice.request")[0]
    card = request["payload"]
    assert request["session_key"] == KEY and card["kind"] == "model"
    # the command as sent rides with the card: a board whose POST has not returned yet draws the
    # sender's bubble ABOVE the card (the stream can be faster than the POST)
    assert card["request"] == "/model" and card["client_message_id"] == "wb-m"
    assert plugin_commands.pending(KEY)[0]["request"] == "/model"
    labels = [o["label"] for o in card["options"]]
    assert "gpt-5.5" in labels
    assert [p["choice_id"] for p in plugin_commands.pending(KEY)] == [card["choice_id"]]

    # another session cannot pick on this card
    assert (await _answer("agent:main:telegram:dm:other", card["choice_id"], 0))[0] == 404
    status, body = await _answer(KEY, card["choice_id"], labels.index("gpt-5.5"))
    assert status == 200 and body["resolved"] is True and "gpt-5.5" in body["text"]
    assert runner._session_model_overrides                       # the session switched
    resolved = _of(_settle(seen, lambda s: _of(s, "choice.resolved")), "choice.resolved")[0]
    assert resolved["id"] == card["choice_id"] and "gpt-5.5" in resolved["payload"]["text"]
    assert (await _answer(KEY, card["choice_id"], 0))[0] == 404   # closed exactly once
    assert plugin_commands.pending(KEY) == []


@pytest.mark.asyncio
async def test_choice_picker_card_runs_the_same_applier(monkeypatch):
    seen = _subscribe()
    applied = []

    async def on_choice(_chat, value):
        applied.append(value)
        return f"gesetzt: {value}"

    async def inner(event):
        surface = plugin_commands.picker_surface()
        assert surface is not None
        await surface.send_choice_picker(chat_id="c1", title="Reasoning", session_key=KEY,
                                         choices=[{"value": "low", "label": "Niedrig"},
                                                  {"value": "high", "label": "Hoch", "is_current": True}],
                                         on_choice_selected=on_choice)
        return None

    await plugin_commands.handle(SimpleNamespace(), _command_event("/reasoning"), inner)
    card = _of(_settle(seen, lambda s: _of(s, "choice.request")), "choice.request")[0]["payload"]
    assert card["options"][1] == {"label": "Hoch", "is_current": True}
    assert "value" not in card["options"][0]                      # the board picks by index only
    assert (await _answer(KEY, card["choice_id"], 9))[0] == 400
    assert (await _answer(KEY, card["choice_id"], 0))[1]["text"] == "gesetzt: low"
    assert applied == ["low"]
    assert plugin_commands.picker_surface() is None              # outside the command: Telegram's


@pytest.mark.asyncio
async def test_a_new_card_replaces_the_open_one_of_its_session():
    seen = _subscribe()

    async def inner(event):
        surface = plugin_commands.picker_surface()
        await surface.send_choice_picker(chat_id="c1", title="t", session_key=KEY,
                                         choices=[{"value": "a"}], on_choice_selected=AsyncMock())

    await plugin_commands.handle(SimpleNamespace(), _command_event("/fast", cmid="wb-a"), inner)
    await plugin_commands.handle(SimpleNamespace(), _command_event("/fast", cmid="wb-b"), inner)
    _settle(seen, lambda s: _of(s, "choice.expired"))
    assert len(plugin_commands.pending(KEY)) == 1
    assert _of(seen, "choice.expired")[0]["payload"]["cause"] == "ersetzt"


@pytest.mark.asyncio
async def test_destructive_confirm_of_new_becomes_a_card_not_telegram_buttons():
    seen = _subscribe()
    runner = _status_runner()
    runner._slash_confirm_counter = None
    ran = []

    async def on_confirm(choice):
        ran.append(choice)
        return "✨ Neue Sitzung."

    async def inner(event):
        return await runner._request_slash_confirm(event=event, command="new", title="/new",
                                                   message="Wirklich neu?", handler=on_confirm)

    assert await plugin_commands.handle(runner, _command_event("/new", cmid="wb-n"), inner) is None
    runner.adapters[Platform.TELEGRAM].send_slash_confirm.assert_not_called()
    card = _of(_settle(seen, lambda s: _of(s, "choice.request")), "choice.request")[0]["payload"]
    assert card["kind"] == "confirm" and len(card["options"]) == 3
    status, body = await _answer(KEY, card["choice_id"], 0)
    assert status == 200 and ran == ["once"] and body["text"] == "✨ Neue Sitzung."


def _confirm_card(seen):
    return _of(_settle(seen, lambda s: _of(s, "choice.request")), "choice.request")[0]["payload"]


async def _open_confirm(runner, ran):
    runner._slash_confirm_counter = None

    async def on_confirm(choice):
        ran.append(choice)
        return "✨ Neue Sitzung."

    async def inner(event):
        return await runner._request_slash_confirm(event=event, command="new", title="/new",
                                                   message="Wirklich neu?", handler=on_confirm)

    await plugin_commands.handle(runner, _command_event("/new", cmid="wb-n"), inner)


@pytest.mark.asyncio
async def test_confirm_card_expires_with_its_slash_confirm_not_after_30_minutes():
    """The confirmation behind the card lives ``slash_confirm.DEFAULT_TIMEOUT_SECONDS`` (300 s):
    the card must close then too, not stay pickable for the generic 30 minutes."""
    from tools import slash_confirm
    seen = _subscribe()
    runner, ran = _status_runner(), []
    await _open_confirm(runner, ran)
    card = _confirm_card(seen)
    with plugin_commands._lock:
        plugin_commands._open[card["choice_id"]].created -= slash_confirm.DEFAULT_TIMEOUT_SECONDS + 1
    assert plugin_commands.pending(KEY) == []                   # gone with its confirmation
    expired = _of(_settle(seen, lambda s: _of(s, "choice.expired")), "choice.expired")
    assert expired[0]["payload"]["cause"] == "abgelaufen"
    assert (await _answer(KEY, card["choice_id"], 0))[0] == 404
    assert ran == []
    slash_confirm.clear(KEY)


@pytest.mark.asyncio
async def test_late_pick_on_a_timed_out_confirmation_says_expired_not_done():
    """slash_confirm dropped the command (stale): ``resolve`` returns None and runs nothing — the
    board must hear "abgelaufen" (404), never a ``choice.resolved`` that looks like success."""
    from tools import slash_confirm
    seen = _subscribe()
    runner, ran = _status_runner(), []
    await _open_confirm(runner, ran)
    card = _confirm_card(seen)
    with slash_confirm._lock:
        slash_confirm._pending[KEY]["created_at"] -= slash_confirm.DEFAULT_TIMEOUT_SECONDS + 1

    status, body = await _answer(KEY, card["choice_id"], 0)

    assert status == 404 and body["grund"] == "Diese Auswahl ist abgelaufen."
    assert ran == []
    _settle(seen, lambda s: _of(s, "choice.expired"))
    assert [e["id"] for e in _of(seen, "choice.expired")] == [card["choice_id"]]
    assert _of(seen, "choice.resolved") == []
    assert plugin_commands.pending(KEY) == []


# ── emergency stop, drain and busy: a board command is a person's message ────────────────────


def _skill_turn_runner(monkeypatch):
    """A runner on which ``/myskill`` would become an agent turn (the skill rewrite)."""
    runner = _status_runner()
    monkeypatch.setattr(runner, "_hm_dispatch_idle_commands", AsyncMock(return_value=(False, None)),
                        raising=False)
    monkeypatch.setattr(runner, "_is_session_running", lambda _k: False, raising=False)
    monkeypatch.setattr(runner, "_hm_evict_idle_stale_agent", lambda _k: None, raising=False)
    runner._external_drain_active = False
    return runner


@pytest.mark.asyncio
async def test_paused_gateway_refuses_a_board_skill_command_with_the_pause_notice(monkeypatch):
    seen = _subscribe()
    runner = _skill_turn_runner(monkeypatch)
    notice = "⏸️ Hermes is paused (wartung). New work is on hold; run `hermes resume` to pick things back up."
    monkeypatch.setattr("agent.estop.paused_reply", lambda: notice)

    assert await runner._handle_message(_command_event("/myskill mach was", cmid="wb-p")) is None

    reply = _of(_settle(seen, lambda s: _of(s, "command.reply")), "command.reply")[0]
    assert reply["id"] == "wb-p" and reply["payload"]["text"] == notice
    runner._handle_message_with_agent.assert_not_called()
    runner._hm_dispatch_idle_commands.assert_not_called()


@pytest.mark.asyncio
async def test_paused_gateway_still_lets_internal_wakes_through(monkeypatch):
    """Unchanged: a synthetic internal event (no board command) is not a new user turn."""
    runner = _skill_turn_runner(monkeypatch)
    monkeypatch.setattr("agent.estop.paused_reply", lambda: "paused")
    event = _command_event("[background done]")
    event.metadata.pop(plugin_commands.COMMAND_FLAG)
    assert runner._hm_estop_gate(event, event.source, True) is None


@pytest.mark.asyncio
async def test_external_drain_refuses_a_board_skill_command(monkeypatch):
    from agent.i18n import t
    seen = _subscribe()
    runner = _skill_turn_runner(monkeypatch)
    monkeypatch.setattr("agent.estop.paused_reply", lambda: None)
    runner._external_drain_active = True

    assert await runner._handle_message(_command_event("/myskill mach was", cmid="wb-d")) is None

    reply = _of(_settle(seen, lambda s: _of(s, "command.reply")), "command.reply")[0]
    assert reply["id"] == "wb-d" and reply["payload"]["text"] == t("gateway.busy.draining_maintenance")
    runner._handle_message_with_agent.assert_not_called()


@pytest.mark.asyncio
async def test_board_skill_command_while_busy_takes_the_chat_busy_path():
    """Like a Telegram ``/myskill`` mid-run (interrupt mode): the running turn is interrupted and the
    command queued — not parked silently like an internal wake. The ack goes to the stream."""
    from tests.gateway.test_busy_session_ack import _make_adapter, _make_runner
    seen = _subscribe()
    runner, _sentinel = _make_runner()
    runner._busy_input_mode = "interrupt"
    adapter = _make_adapter()
    runner.adapters[Platform.TELEGRAM] = adapter
    agent = MagicMock()
    agent.get_activity_summary.return_value = {
        "api_call_count": 3, "max_iterations": 60, "current_tool": "terminal",
        "last_activity_ts": time.time(), "last_activity_desc": "terminal", "seconds_since_activity": 1.0}
    runner._running_agents[KEY] = agent
    runner._running_agents_ts[KEY] = time.time() - 60
    queued = []
    runner._queue_or_replace_pending_event = lambda key, ev: queued.append((key, ev))

    event = _command_event("/myskill mach was", cmid="wb-b")
    assert await runner._handle_active_session_busy_message(event, KEY) is True

    agent.interrupt.assert_called_once_with("/myskill mach was")
    assert queued == [(KEY, event)]
    adapter._send_with_retry.assert_not_called()                 # nothing to the chat
    reply = _of(_settle(seen, lambda s: _of(s, "command.reply")), "command.reply")[0]
    assert reply["id"] == "wb-b" and reply["payload"]["text"]

"""WB-1301: an expired approval card offers "🔁 Nochmal ausführen" (and a "Nochmal" text reply).

The REAL chain is driven: ``terminal`` handler → command guard → gateway approval wait (times
out) → timeout settle hook → offer → Telegram card edit. Execution is stubbed at the backend
(``_run_foreground``), so nothing real runs; every call is recorded in ``runs``.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import run_turn_runner_approval_settle as settle_mod
from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource
from plugins.platforms.telegram import adapter as tg
from tools import approval as A
from tools import approval_context, approval_retry

KEY = "agent:main:telegram:dm:wb1301:51"
DANGEROUS = "rm -rf /tmp/hermes-wb1301-probe-never-created"
OWNER = "4711"
CB = {"chat_id": "12345", "thread_id": None}


@pytest.fixture(autouse=True)
def gateway_turn(monkeypatch):
    from gateway.session_context import clear_session_vars, set_session_vars

    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    for name in ("HERMES_INTERACTIVE", "HERMES_GATEWAY_SESSION", "HERMES_CRON_SESSION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 0)
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr("tools.approval_prompt._read_choice",
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("CLI prompt reached")))

    def scrub():
        with A._lock:
            for table in (A._gateway_queues, A._gateway_notify_cbs, A._gateway_notify_leases, A._session_approved):
                table.pop(KEY, None)
        approval_retry._reset_for_tests()

    scrub()
    tokens = set_session_vars(platform="telegram", source="telegram", chat_id="12345", session_key=KEY)
    key_token = approval_context.set_current_session_key(KEY)
    try:
        yield
    finally:
        approval_context.reset_current_session_key(key_token)
        clear_session_vars(tokens)
        scrub()


@pytest.fixture(autouse=True)
def keyboard_fakes(monkeypatch):
    """python-telegram-bot is not installed in the test env: record buttons as plain objects."""
    class Button:
        def __init__(self, text, callback_data=None):
            self.text, self.callback_data = text, callback_data

    class Markup:
        def __init__(self, rows):
            self.inline_keyboard = [list(row) for row in rows]

    monkeypatch.setattr(tg, "InlineKeyboardButton", Button)
    monkeypatch.setattr(tg, "InlineKeyboardMarkup", Markup)


@pytest.fixture
def runs(monkeypatch):
    """Stub the terminal backend; the guard in between stays real."""
    from tools import terminal_tool as TT
    calls = []

    def run_foreground(command, *_a, **_k):
        calls.append(command)
        return json.dumps({"output": "probe-out", "exit_code": 0, "error": None})

    monkeypatch.setattr(TT, "_acquire_env", lambda *_a, **_k: object())
    monkeypatch.setattr(TT, "_pre_exec_block", lambda *_a, **_k: None)
    monkeypatch.setattr(TT, "_run_foreground", run_foreground)
    return calls


def make_adapter(*, live_session="sess-1"):
    a = tg.TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    a._bot = AsyncMock()
    a._callback_authorized = AsyncMock(return_value=True)
    a.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="99"))
    a.send_document = AsyncMock(return_value=SimpleNamespace(success=True))

    async def handle_message(event):
        event._gateway_accepted = True
    a.handle_message = AsyncMock(side_effect=handle_message)
    a._session_store = SimpleNamespace(peek_session_id=lambda key: live_session if key == KEY else None)
    return a


class Runner:
    def __init__(self, adapter):
        self.scheduled = []
        self._ctx = SimpleNamespace(
            session_key=KEY, session_id="sess-1", _run_still_current=lambda: True,
            source=SessionSource(platform=Platform.TELEGRAM, chat_id="12345", user_id=OWNER, chat_type="dm"),
            _status_adapter=adapter, _status_chat_id="12345", _status_thread_metadata=None)

    def _schedule(self, coro, label):
        self.scheduled.append(coro)
        return None


async def expire(adapter, command=DANGEROUS, *, deny=False, child=None):
    """Run the real terminal handler until its approval times out (or is denied in the gap)."""
    from tools import terminal_tool as TT
    runner = Runner(adapter)

    def notify(data):
        adapter._approval_state["old-card"] = {
            "session_key": KEY, "request_id": data["request_id"], "choices": {"once", "deny"},
            "chat_id": "12345", "thread_id": None, "message_id": "42"}
        settle_mod.register_timeout_notice(runner, data, command=data["command"], card_message_id="42")
        if deny:
            A.resolve_gateway_approval(KEY, "deny", request_id=data["request_id"])

    def call():
        token = None
        if child:
            from agent.delegation_context import _DELEGATED_CHILD_CONTEXT
            token = _DELEGATED_CHILD_CONTEXT.set(True)
        try:
            return TT._handle_terminal({"command": command}, task_id=child or "task-main")
        finally:
            if token is not None:
                _DELEGATED_CHILD_CONTEXT.reset(token)

    A.register_gateway_notify(KEY, notify)
    try:
        out = await asyncio.to_thread(call)
    finally:
        A.unregister_gateway_notify(KEY)
    for coro in runner.scheduled:
        await coro
    return out


def retry_token(adapter):
    for call in reversed(adapter._bot.edit_message_text.await_args_list):
        markup = call.kwargs.get("reply_markup")
        if markup is not None:
            return markup.inline_keyboard[0][0].callback_data.split(":", 2)[2]
    return None


def press(message_id=42, user_id=OWNER):
    return SimpleNamespace(message=SimpleNamespace(message_id=message_id), answer=AsyncMock(),
                           from_user=SimpleNamespace(id=user_id, first_name="Owner"))


async def settle_tasks():
    while tg._SETTLE_TASKS:
        await asyncio.gather(*list(tg._SETTLE_TASKS))


def last_card_text(adapter):
    return adapter._bot.edit_message_text.await_args_list[-1].kwargs["text"]


# --- card ------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_turns_the_card_into_a_retry_card(runs):
    a = make_adapter()
    out = await expire(a)
    assert "BLOCKED" in out and runs == []                       # the agent still got the timeout
    call = a._bot.edit_message_text.await_args_list[-1]
    assert call.kwargs["message_id"] == 42
    rows = call.kwargs["reply_markup"].inline_keyboard
    assert [b.text for row in rows for b in row] == ["🔁 Nochmal ausführen", "Verwerfen"]
    assert rows[0][0].callback_data.startswith("er:r:") and rows[1][0].callback_data.startswith("er:x:")
    assert "Freigabe abgelaufen" in call.kwargs["text"] and DANGEROUS in call.kwargs["text"]
    assert "old-card" not in a._approval_state                   # the old once/deny buttons are gone


# --- press → run → deliver -------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_press_runs_once_and_delivers_to_the_live_session(runs):
    a = make_adapter()
    await expire(a)
    await a._handle_approval_retry_callback(press(), "er:r:" + retry_token(a), CB)
    await settle_tasks()
    assert runs == [DANGEROUS]
    event = a.handle_message.await_args.args[0]
    assert event.internal and event.text.startswith("[IMPORTANT: Der Owner hat eine abgelaufene Freigabe")
    assert "probe-out" in event.text and "Exit-Code: 0" in event.text
    a.send.assert_not_awaited()
    assert "Ergebnis an die Sitzung geliefert" in last_card_text(a)


@pytest.mark.asyncio
async def test_ended_session_gets_output_and_file_in_the_chat(runs):
    a = make_adapter(live_session="a-newer-session")
    await expire(a)
    await a._handle_approval_retry_callback(press(), "er:r:" + retry_token(a), CB)
    await settle_tasks()
    assert runs == [DANGEROUS]
    a.handle_message.assert_not_awaited()
    text = a.send.await_args.args[1]
    assert "Exit-Code: 0" in text and "probe-out" in text
    a.send_document.assert_awaited_once()
    assert "Ergebnis steht unten im Chat" in last_card_text(a)


@pytest.mark.asyncio
async def test_live_subagent_gets_the_result_by_steer(runs):
    from tools import delegate_tool_registry as reg
    steered = []
    agent = SimpleNamespace(steer=lambda text: steered.append(text) or True, session_id="child-sess")
    reg._register_subagent({"subagent_id": "sa-1301", "agent": agent})
    try:
        a = make_adapter()
        await expire(a, child="sa-1301")
        await a._handle_approval_retry_callback(press(), "er:r:" + retry_token(a), CB)
        await settle_tasks()
    finally:
        reg._unregister_subagent("sa-1301")
    assert runs == [DANGEROUS]
    assert len(steered) == 1 and "probe-out" in steered[0] and steered[0].startswith("[IMPORTANT:")
    a.handle_message.assert_not_awaited()
    a.send.assert_not_awaited()
    assert "an den Subagenten geliefert" in last_card_text(a)


@pytest.mark.asyncio
async def test_second_press_does_nothing(runs):
    a = make_adapter()
    await expire(a)
    token = retry_token(a)
    await a._handle_approval_retry_callback(press(), "er:r:" + token, CB)
    await settle_tasks()
    second = press()
    await a._handle_approval_retry_callback(second, "er:r:" + token, CB)
    await settle_tasks()
    assert runs == [DANGEROUS]
    assert "Schon erledigt" in second.answer.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_foreign_sender_is_refused_and_the_owner_still_can(runs):
    a = make_adapter()
    await expire(a)
    token = retry_token(a)
    a._callback_authorized = AsyncMock(return_value=False)        # not on the callback allowlist
    await a._handle_approval_retry_callback(press(user_id="999"), "er:r:" + token, CB)
    a._callback_authorized = AsyncMock(return_value=True)
    intruder = press(user_id="999")                               # allowed in the chat, not the session owner
    await a._handle_approval_retry_callback(intruder, "er:r:" + token, CB)
    await settle_tasks()
    assert runs == []
    assert "Nur der Owner" in intruder.answer.await_args.kwargs["text"]
    await a._handle_approval_retry_callback(press(), "er:r:" + token, CB)
    await settle_tasks()
    assert runs == [DANGEROUS]


@pytest.mark.asyncio
async def test_press_on_another_message_does_not_run(runs):
    a = make_adapter()
    await expire(a)
    await a._handle_approval_retry_callback(press(message_id=77), "er:r:" + retry_token(a), CB)
    await settle_tasks()
    assert runs == []


@pytest.mark.asyncio
async def test_button_is_invalid_after_24_hours(runs, monkeypatch):
    a = make_adapter()
    await expire(a)
    token = retry_token(a)
    born = approval_retry.get_offer(token).created_at
    monkeypatch.setattr(approval_retry.time, "time", lambda: born + approval_retry.RETRY_TTL_S + 1)
    q = press()
    await a._handle_approval_retry_callback(q, "er:r:" + token, CB)
    await settle_tasks()
    assert runs == []
    assert "Abgelaufen (24 h)" in q.answer.await_args.kwargs["text"]
    assert "abgelaufen" in last_card_text(a) and a._bot.edit_message_text.await_args.kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_callback_allowlist_is_checked_before_anything_runs(runs):
    """Same sender check as normal approvals: a tap the callback allowlist refuses never runs,
    even when it carries the owner's id (second defence layer switched off by construction)."""
    a = make_adapter()
    await expire(a)
    token = retry_token(a)
    a._callback_authorized = AsyncMock(return_value=False)
    await a._handle_approval_retry_callback(press(user_id=OWNER), "er:r:" + token, CB)
    await settle_tasks()
    assert runs == [] and approval_retry.get_offer(token).state == "open"
    a._callback_authorized.assert_awaited_once()


@pytest.mark.asyncio
async def test_drop_button_never_runs(runs):
    a = make_adapter()
    await expire(a)
    token = retry_token(a)
    await a._handle_approval_retry_callback(press(), "er:x:" + token, CB)
    await a._handle_approval_retry_callback(press(), "er:r:" + token, CB)
    await settle_tasks()
    assert runs == []
    assert "Verworfen" in last_card_text(a)


# --- text reply ------------------------------------------------------------------------------------

def reply_msg(text, *, user_id=OWNER, reply_to=42):
    return SimpleNamespace(text=text, chat_id=12345, chat=SimpleNamespace(id=12345), message_thread_id=None,
                           reply_to_message=SimpleNamespace(message_id=reply_to),
                           from_user=SimpleNamespace(id=user_id, first_name="Owner"))


@pytest.mark.asyncio
async def test_text_reply_nochmal_works_like_the_button(runs, monkeypatch):
    a = make_adapter()
    await expire(a)
    msg = reply_msg("Nochmal")
    monkeypatch.setattr(a, "_effective_update_message", lambda update: msg)
    monkeypatch.setattr(a, "_is_user_authorized_from_message", lambda m: True)
    enqueued = []
    monkeypatch.setattr(a, "_enqueue_text_event", enqueued.append)
    await a._handle_text_message(SimpleNamespace(message=msg), None)
    await settle_tasks()
    assert runs == [DANGEROUS] and enqueued == []
    assert a.handle_message.await_args.args[0].internal
    # a second "Nochmal" is answered, not run again
    assert await a._maybe_approval_retry_reply(reply_msg("retry")) is True
    await settle_tasks()
    assert runs == [DANGEROUS]


@pytest.mark.asyncio
@pytest.mark.parametrize("text,reply_to,user", [("Was war das?", 42, OWNER), ("Nochmal", 41, OWNER),
                                                ("Nochmal", 42, "999")])
async def test_other_replies_stay_ordinary_messages(runs, text, reply_to, user):
    a = make_adapter()
    await expire(a)
    assert await a._maybe_approval_retry_reply(reply_msg(text, reply_to=reply_to, user_id=user)) is False
    await settle_tasks()
    assert runs == []


# --- never more than a normal approval -------------------------------------------------------------

@pytest.mark.asyncio
async def test_active_deny_gets_no_button_even_in_the_deadline_gap(runs):
    a = make_adapter()
    out = await expire(a, deny=True)
    assert "BLOCKED" in out
    assert retry_token(a) is None and approval_retry._offers == {}


def test_never_approvable_commands_get_no_offer():
    data = {"command": "rm -rf /", "request_id": "r1"}
    with approval_retry.retry_target("terminal", "rm -rf /", lambda: "{}"):
        assert approval_retry.offer_for_timeout(KEY, data) is None
    with approval_retry.retry_target("terminal", DANGEROUS, lambda: "{}"):
        assert approval_retry.offer_for_timeout(KEY, {"command": DANGEROUS, "request_id": "r2"}) is not None
        # a gate inside the call that is not this command (plugin rule, write gate): no offer
        assert approval_retry.offer_for_timeout(KEY, {"command": "<write_file> (plugin approval rule)"}) is None
    assert approval_retry.offer_for_timeout(KEY, {"command": DANGEROUS}) is None   # no target at all


@pytest.mark.asyncio
async def test_retry_reruns_the_floors(runs, monkeypatch):
    """A rule added after the timeout (approvals.deny) still blocks the re-run: the button grants
    exactly what a normal approval would, nothing more."""
    a = make_adapter(live_session="gone")
    await expire(a)
    monkeypatch.setattr(A, "_match_user_deny_rule", lambda command: "rm -rf /tmp/*" if command == DANGEROUS else None)
    await a._handle_approval_retry_callback(press(), "er:r:" + retry_token(a), CB)
    await settle_tasks()
    assert runs == []
    assert "Exit-Code" in a.send.await_args.args[1]


def test_grant_is_one_shot_and_exact():
    grant = approval_retry._Grant(DANGEROUS)
    token = approval_retry._grant.set(grant)
    try:
        assert approval_retry.consume_grant(DANGEROUS + " ") is False
        assert approval_retry.consume_grant(DANGEROUS) is True
        assert approval_retry.consume_grant(DANGEROUS) is False
    finally:
        approval_retry._grant.reset(token)
    assert approval_retry.consume_grant(DANGEROUS) is False


@pytest.mark.parametrize("text,expected", [("Nochmal", True), ("nochmal!", True), ("Retry", True),
                                           ("ja", True), (" Ja. ", True), ("ja, aber anders", False),
                                           ("nein", False), ("", False), (None, False)])
def test_retry_words(text, expected):
    assert approval_retry.is_retry_word(text) is expected

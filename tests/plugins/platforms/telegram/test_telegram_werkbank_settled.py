"""WB-1129: a Telegram card answered in the Werkbank shows „erledigt in der Werkbank“.

Fake bot: ``_bot`` is an ``AsyncMock``; the card edit is the ``edit_message_text`` call it records.
The session events arrive like in production: ``on_gateway_session_event`` is called from a worker
thread (``gateway.session_events``), never on the adapter's loop.
"""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.gateway.conftest import _ensure_telegram_mock

_ensure_telegram_mock()                     # the telegram package as tests/gateway sees it

from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.telegram import adapter as tg
from plugins.platforms.telegram.adapter import TelegramAdapter
from tools import approval
from tools import clarify_gateway

KEY = "agent:main:telegram:dm:12345:51"


@pytest.fixture(autouse=True)
def _clean():
    def scrub():
        with approval._lock:
            approval._gateway_queues.pop(KEY, None)
            getattr(approval, "_recent_decisions", {}).clear()
        with clarify_gateway._lock:
            for cid in clarify_gateway._session_index.pop(KEY, []):
                clarify_gateway._entries.pop(cid, None)
    scrub()
    yield
    scrub()


def make_adapter():
    a = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    a._bot = AsyncMock()
    ids = iter(range(42, 100))
    a._bot.send_message.side_effect = lambda **_k: SimpleNamespace(message_id=next(ids), message_thread_id=51)
    a._callback_authorized = AsyncMock(return_value=True)
    return a


def pend(rid, **data):
    entry = SimpleNamespace(event=threading.Event(), data={"request_id": rid, **data}, result=None,
                            reason=None, decided_by=None)
    with approval._lock:
        approval._gateway_queues.setdefault(KEY, []).append(entry)
    return entry


def query(message_id):
    return SimpleNamespace(message=SimpleNamespace(message_id=message_id, text="❓ Welche?"),
                           answer=AsyncMock(), edit_message_text=AsyncMock(),
                           from_user=SimpleNamespace(first_name="tester"))


def resolved_event(type_, payload, key=KEY):
    return {"v": 1, "session_key": key, "session_id": "sid", "platform": "telegram", "origin": "telegram",
            "core_seq": 1, "type": type_, "id": payload.get("request_id") or payload.get("clarify_id"),
            "parent_id": None, "ts": "2026-10-14T10:00:00Z", "payload": payload}


async def deliver(event):
    """Like gateway.session_events: the hook runs on a worker thread, the edit on the adapter loop."""
    await asyncio.to_thread(tg.on_gateway_session_event, event=event)
    for _ in range(50):
        await asyncio.sleep(0.01)


def edits(a):
    return [c.kwargs for c in a._bot.edit_message_text.await_args_list]


# ── Fertig, wenn 4: the card is edited after a Werkbank answer ──────────────────────────────


@pytest.mark.asyncio
async def test_werkbank_answer_edits_the_card_drops_buttons_and_forgets_it():
    a = make_adapter()
    pend("req-A")
    await a.send_exec_approval("12345", "rm -rf x", KEY, metadata={"approval_request_id": "req-A", "thread_id": "51"})
    token = next(iter(a._approval_state))
    approval.resolve_gateway_approval(KEY, "once", request_id="req-A", decided_by="werkbank")

    await deliver(resolved_event("approval.resolved", {"request_id": "req-A", "choice": "once",
                                                      "decided_by": "werkbank"}))
    [edit] = edits(a)
    assert edit["chat_id"] == 12345 and edit["message_id"] == 42
    assert "erledigt in der Werkbank: einmal erlaubt" in edit["text"]
    assert edit["reply_markup"] is None
    assert token not in a._approval_state

    # a late tap on the edited card: no resolve, the same outcome as a toast
    q = query(42)
    await a._handle_exec_approval_callback(q, "ea:deny:" + token, {"chat_id": "12345", "thread_id": "51"})
    assert "erledigt in der Werkbank: einmal erlaubt" in q.answer.await_args.kwargs["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("choice,word", [("deny", "abgelehnt"), ("session", "für die Sitzung erlaubt"),
                                         ("always", "immer erlaubt")])
async def test_card_names_the_werkbank_choice(choice, word):
    a = make_adapter()
    pend("req-A")
    await a.send_exec_approval("12345", "rm -rf x", KEY, metadata={"approval_request_id": "req-A", "thread_id": "51"})
    await deliver(resolved_event("approval.resolved", {"request_id": "req-A", "choice": choice,
                                                      "decided_by": "werkbank"}))
    assert f"erledigt in der Werkbank: {word}" in edits(a)[0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,key", [
    ({"request_id": "req-A", "choice": "once", "decided_by": "telegram"}, KEY),   # Telegram itself
    ({"request_id": "req-A", "choice": "once", "decided_by": None}, KEY),         # /approve text
    ({"request_id": "req-OTHER", "choice": "once", "decided_by": "werkbank"}, KEY),
    ({"request_id": "req-A", "choice": "once", "decided_by": "werkbank"}, "agent:main:telegram:dm:1:2"),
])
async def test_other_events_leave_the_card_alone(payload, key):
    a = make_adapter()
    pend("req-A")
    await a.send_exec_approval("12345", "rm -rf x", KEY, metadata={"approval_request_id": "req-A", "thread_id": "51"})
    token = next(iter(a._approval_state))
    await deliver(resolved_event("approval.resolved", payload, key=key))
    assert edits(a) == []
    assert token in a._approval_state


@pytest.mark.asyncio
async def test_two_cards_only_the_named_request_is_edited():
    a = make_adapter()
    pend("req-A"), pend("req-B")
    await a.send_exec_approval("12345", "a", KEY, metadata={"approval_request_id": "req-A", "thread_id": "51"})
    await a.send_exec_approval("12345", "b", KEY, metadata={"approval_request_id": "req-B", "thread_id": "51"})
    await deliver(resolved_event("approval.resolved", {"request_id": "req-B", "choice": "deny",
                                                      "decided_by": "werkbank"}))
    [edit] = edits(a)
    assert edit["message_id"] == 43
    assert [s["request_id"] for s in a._approval_state.values()] == ["req-A"]


# ── Fertig, wenn 2: Telegram and Werkbank at once — one wins, both show the same ─────────────


@pytest.mark.asyncio
async def test_tap_after_werkbank_won_shows_the_werkbank_result_not_expired():
    a = make_adapter()
    entry = pend("req-A")
    await a.send_exec_approval("12345", "rm -rf x", KEY, metadata={"approval_request_id": "req-A", "thread_id": "51"})
    token = next(iter(a._approval_state))
    # the Werkbank resolved first; its event has not reached the adapter yet
    assert approval.resolve_gateway_approval(KEY, "once", request_id="req-A", decided_by="werkbank") == 1
    a._edit_md_quiet = AsyncMock()
    q = query(42)
    await a._handle_exec_approval_callback(q, "ea:deny:" + token, {"chat_id": "12345", "thread_id": "51"})
    assert entry.result == "once" and entry.decided_by == "werkbank"          # the tap changed nothing
    shown = a._edit_md_quiet.await_args.args[1]
    assert "erledigt in der Werkbank: einmal erlaubt" in shown
    assert "erledigt in der Werkbank: einmal erlaubt" in q.answer.await_args.kwargs["text"]
    # the event arriving afterwards does not edit a second time
    await deliver(resolved_event("approval.resolved", {"request_id": "req-A", "choice": "once",
                                                      "decided_by": "werkbank"}))
    assert edits(a) == []


@pytest.mark.asyncio
async def test_concurrent_tap_and_werkbank_exactly_one_wins_and_both_agree():
    for round_ in range(10):
        a = make_adapter()
        rid = f"req-{round_}"
        entry = pend(rid)
        await a.send_exec_approval("12345", "rm", KEY, metadata={"approval_request_id": rid, "thread_id": "51"})
        token = next(iter(a._approval_state))
        a._edit_md_quiet = AsyncMock()
        q = query(42)
        werkbank = asyncio.to_thread(approval.resolve_gateway_approval, KEY, "once", request_id=rid,
                                     decided_by="werkbank")
        tap = a._handle_exec_approval_callback(q, "ea:deny:" + token, {"chat_id": "12345", "thread_id": "51"})
        counts = await asyncio.gather(werkbank, tap)
        decision = approval.gateway_approval_decision(KEY, rid)
        winner = decision["decided_by"]
        assert winner in ("telegram", "werkbank")
        assert counts[0] == (1 if winner == "werkbank" else 0)
        assert entry.result == decision["choice"] and entry.decided_by == winner
        await deliver(resolved_event("approval.resolved", {"request_id": rid, **decision}))
        texts = [c.args[1] for c in a._edit_md_quiet.await_args_list] + [e["text"] for e in edits(a)]
        assert len(texts) == 1                                               # one edit, never two
        if winner == "werkbank":
            assert "erledigt in der Werkbank: einmal erlaubt" in texts[0]
        else:
            assert "tester" in texts[0] and "werkbank" not in texts[0].lower()


# ── clarify cards ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clarify_card_edited_after_werkbank_answer():
    a = make_adapter()
    entry = clarify_gateway.register("clar-1", KEY, "Welche?", ["A", "B"])
    await a.send_clarify("12345", "Welche?", ["A", "B"], "clar-1", KEY, metadata={"thread_id": "51"})
    assert clarify_gateway.resolve_gateway_clarify("clar-1", "B", decided_by="werkbank")
    await deliver(resolved_event("clarify.resolved", {"clarify_id": "clar-1", "answer": "B",
                                                     "skipped": False, "decided_by": "werkbank"}))
    [edit] = edits(a)
    assert edit["message_id"] == 42 and edit["reply_markup"] is None
    assert "erledigt in der Werkbank" in edit["text"] and "B" in edit["text"] and "Welche?" in edit["text"]
    assert "clar-1" not in a._clarify_state
    assert entry.decided_by == "werkbank"


@pytest.mark.asyncio
async def test_clarify_button_names_telegram_as_decider():
    a = make_adapter()
    entry = clarify_gateway.register("clar-1", KEY, "Welche?", ["A", "B"])
    await a.send_clarify("12345", "Welche?", ["A", "B"], "clar-1", KEY, metadata={"thread_id": "51"})
    await a._handle_clarify_callback(query(42), "cl:clar-1:0", {"chat_id": "12345", "thread_id": "51"})
    assert entry.response == "A" and entry.decided_by == "telegram"


@pytest.mark.asyncio
async def test_telegram_tap_resolves_with_decided_by_telegram():
    a = make_adapter()
    entry = pend("req-A")
    await a.send_exec_approval("12345", "rm", KEY, metadata={"approval_request_id": "req-A", "thread_id": "51"})
    token = next(iter(a._approval_state))
    a._edit_md_quiet = AsyncMock()
    await a._handle_exec_approval_callback(query(42), "ea:once:" + token, {"chat_id": "12345", "thread_id": "51"})
    assert entry.result == "once" and entry.decided_by == "telegram"
    assert approval.gateway_approval_decision(KEY, "req-A") == {"choice": "once", "decided_by": "telegram"}


def test_hook_is_registered_only_with_the_werkbank_plugin_enabled(monkeypatch):
    registered = []
    ctx = SimpleNamespace(register_platform=lambda **_k: None,
                          register_hook=lambda name, cb: registered.append((name, cb)))
    monkeypatch.setattr(tg, "_werkbank_answers_enabled", lambda _ctx: False)
    tg.register(ctx)
    assert registered == []
    monkeypatch.setattr(tg, "_werkbank_answers_enabled", lambda _ctx: True)
    tg.register(ctx)
    assert registered == [("on_gateway_session_event", tg.on_gateway_session_event)]

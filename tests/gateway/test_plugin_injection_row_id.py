"""WB-1128: a plugin-injected message's ``client_message_id`` lands on its own SessionDB user row.

The id rides ``persist_user_display_metadata`` (stored as the row's ``display_metadata``), so a
reader names the row exactly — never by its text. ``message_id`` / ``inbound_message_id`` stay
untouched: they are the platform's reply anchor and delivery-ledger key (Telegram unchanged).
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run_plugin_injection import client_message_metadata
from gateway.session import SessionEntry, SessionSource

KEY = "agent:main:telegram:group:-1001:12345"


def _source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="-1001", chat_type="group", user_id="12345")


def _plugin_event(cmid="wb-1"):
    # Route pins (gateway_session_key/_id) are left out: this test drives the turn, not the routing.
    meta = {"hermes_plugin_id": "platforms/werkbank", "hermes_plugin_injection": True}
    if cmid:
        meta["client_message_id"] = cmid
    return MessageEvent(text="aus der Werkbank", source=_source(), internal=True, metadata=meta)


def _telegram_event():
    # A human message can never claim a client id, even if metadata somehow carries one.
    return MessageEvent(text="hi", source=_source(), message_id="777", metadata={"client_message_id": "spoof"})


def test_client_message_metadata_only_for_plugin_injections():
    assert client_message_metadata(_plugin_event()) == {"client_message_id": "wb-1",
                                                        "hermes_plugin_id": "platforms/werkbank"}
    assert client_message_metadata(_plugin_event(cmid=None)) == {}
    assert client_message_metadata(_telegram_event()) == {}
    internal_no_plugin = MessageEvent(text="wake", source=_source(), internal=True,
                                      metadata={"client_message_id": "x"})
    assert client_message_metadata(internal_no_plugin) == {}
    assert client_message_metadata(None) == {}


def _runner(monkeypatch, tmp_path):
    runner = gateway_run.GatewayRunner(GatewayConfig())
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._handle_active_session_busy_message = AsyncMock(return_value=False)
    runner._session_db = MagicMock()
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._is_session_run_current = lambda _key, _gen: True
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_a, **_kw: False
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key=KEY, session_id="sid", created_at=datetime.now(), updated_at=datetime.now(),
        platform=Platform.TELEGRAM, chat_type="group")
    runner.session_store.load_transcript.return_value = []
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._run_agent = AsyncMock(return_value={
        "final_response": "ok", "messages": [{"role": "user", "content": "x"}, {"role": "assistant", "content": "ok"}],
        "tools": [], "history_offset": 0, "last_prompt_tokens": 0, "api_calls": 1, "failed": False})
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    monkeypatch.setattr("agent.model_metadata.get_model_context_length", lambda *_a, **_kw: 100_000)
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("make, expected, inbound", [
    (_plugin_event, "wb-1", None),
    (_telegram_event, None, "777"),
])
async def test_turn_persists_the_client_message_id_on_its_user_row(monkeypatch, tmp_path, make, expected, inbound):
    runner = _runner(monkeypatch, tmp_path)
    await runner._handle_message_with_agent(make(), _source(), KEY, 1)
    kwargs = runner._run_agent.await_args.kwargs
    assert (kwargs["persist_user_display_metadata"] or {}).get("client_message_id") == expected
    assert kwargs["inbound_message_id"] == inbound          # reply anchor / ledger key unchanged


@pytest.mark.asyncio
async def test_queued_followup_persists_the_client_message_id(monkeypatch, tmp_path):
    runner = _runner(monkeypatch, tmp_path)
    runner._MAX_INTERRUPT_DEPTH = 8
    runner._run_agent = AsyncMock(return_value={"final_response": "ok", "messages": []})
    runner._is_goal_continuation_event = MagicMock(return_value=False)
    runner._session_key_for_source = MagicMock(return_value=KEY)
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value="follow-up")
    runner._delivery_adapter_for = MagicMock(return_value=None)
    runner._refresh_agent_cache_message_count = AsyncMock()
    turn_ctx = SimpleNamespace(
        source=_source(), session_id="sid", session_key=KEY, run_generation=1, _interrupt_depth=0, history=[],
        _status_thread_metadata=None, context_prompt=None, result_holder=[None])
    pending = _plugin_event("wb-2")
    await gateway_run.GatewayRunner._run_agent_queued_followup(
        runner, turn_ctx, adapter=None, pending="follow-up", pending_event=pending,
        response="resp", result={"interrupted": True, "messages": []}, stream_task=None)
    kwargs = runner._run_agent.await_args.kwargs
    assert kwargs["persist_user_display_metadata"]["client_message_id"] == "wb-2"
    assert kwargs["inbound_message_id"] is None


# ── turn.done names the row of its final answer (WB-1128 review 3) ─────────────────────────────


def _done_payload(result):
    from gateway import session_events

    sent = []
    scope = session_events.TurnScope(session_key=KEY, session_id="s1", platform="telegram", turn_id="turn_x")
    scope.emit = lambda type_, id_=None, parent_id=None, payload=None: sent.append((type_, payload))
    session_events.end_turn(scope, result=result)
    assert [t for t, _ in sent] == ["turn.done"]
    return sent[0][1]


def test_turn_done_carries_the_message_uid_and_row_id_of_the_answer_row(tmp_path):
    from contextlib import closing
    from pathlib import Path

    from hermes_state import SessionDB

    with closing(SessionDB(db_path=Path(tmp_path) / "state.db")) as db:
        db.create_session("s1", "telegram", model="test/model")
        messages = [{"role": "user", "content": "frage"},
                    {"role": "assistant", "content": "", "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
                    {"role": "tool", "content": "ok", "tool_call_id": "c1"},
                    {"role": "assistant", "content": "Antwort"}]
        db.append_messages_batch("s1", messages)
        row = [r for r in db.get_messages("s1") if r["role"] == "assistant" and r["content"] == "Antwort"][0]
    payload = _done_payload({"final_response": "Antwort", "messages": messages})
    assert payload["message_uid"] == row["message_uid"] and payload["row_id"] == row["id"]


def test_turn_done_names_no_row_when_the_turn_wrote_no_answer():
    # the last assistant text belongs to an EARLIER turn: never borrowed
    history = [{"role": "assistant", "content": "alt", "message_uid": "u-alt", "_row_id": 3},
               {"role": "user", "content": "neu", "message_uid": "u-neu", "_row_id": 4}]
    payload = _done_payload({"final_response": "x", "messages": history})
    assert "message_uid" not in payload and "row_id" not in payload
    # not persisted yet: no uid on the dict, nothing named
    payload = _done_payload({"final_response": "x", "messages": [{"role": "assistant", "content": "x"}]})
    assert "message_uid" not in payload and "row_id" not in payload


# ── compression keeps the sender's id on a carried user turn ───────────────────────────────────


def test_compression_live_projection_keeps_client_message_id_and_plugin_id():
    from agent.context_compressor import user_originated_turn_view
    from tests.agent.test_reference_handoff_active_turn import _composite_handoff

    carrier = _composite_handoff()
    carrier["display_metadata"] = {"client_message_id": "wb-7", "hermes_plugin_id": "platforms/werkbank",
                                   "gateway_input_owner": "x"}
    live = user_originated_turn_view(carrier)
    assert live["display_metadata"] == {"client_message_id": "wb-7", "hermes_plugin_id": "platforms/werkbank"}

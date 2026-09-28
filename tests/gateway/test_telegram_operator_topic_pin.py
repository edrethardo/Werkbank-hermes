"""Operator-pinned Telegram DM topics: ``extra.dm_topics[].topics[].session_id``.

A config-declared DM topic can be bound to an EXISTING Hermes session (e.g. a project admin chat
that also runs in the TUI/dashboard). Authority is config.yaml, not chat input, so:

- the topic resolves to that session without /topic mode,
- the binding row mirrors the pin (managed_mode="operator"),
- /new, /resume and /topic <id> cannot move the topic away,
- a pin naming a session that does not exist in this profile refuses the turn instead of silently
  starting a new session,
- a compressed session is followed to its compression tip,
- the chat-input /topic <id> IDOR guard is unchanged for unpinned topics.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.session import build_session_key
from hermes_state import SessionDB
from tests.gateway.test_telegram_topic_mode import _make_event, _make_runner, _make_source

CHAT = "208214988"
PINNED_THREAD = "500"
FREE_THREAD = "600"


class _PinAdapter:
    """Minimal stand-in for TelegramAdapter's DM-topic lookup (a real class, so the runner's
    class-level ``_get_dm_topic_info`` lookup finds it)."""

    def __init__(self, topics):
        self._topics = topics  # thread_id -> topic config dict
        self._dm_topic_chat_ids = {CHAT}
        self._pending_messages = {}
        self._active_sessions = {}
        self.send = AsyncMock()
        self.send_image_file = AsyncMock()
        self._bot = None
        self._create_dm_topic = AsyncMock(return_value=None)
        self.rename_dm_topic = AsyncMock()

    def _get_dm_topic_info(self, chat_id, thread_id):
        if str(chat_id) != CHAT:
            return None
        return self._topics.get(str(thread_id))


def _runner_with_pin(db, topics, monkeypatch):
    import gateway.run as gateway_run

    runner = _make_runner(session_db=db)
    # The shared fixture stubs the release, which leaves the first turn's running slot occupied;
    # these tests send several messages in a row, so use the real release.
    from gateway.run import GatewayRunner
    runner._release_running_agent_state = GatewayRunner._release_running_agent_state.__get__(runner)
    adapter = _PinAdapter(topics)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._delivery_adapter_for = lambda _source: adapter
    runner._agent_cache_lock = None
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr("hermes_cli.tips.get_random_tip", lambda: "pinned tip for test")
    captured = {}

    async def fake_run_agent(*_args, **kwargs):
        captured.setdefault("session_ids", []).append(kwargs.get("session_id"))
        return {
            "success": True, "final_response": "pinned reply",
            "session_id": kwargs.get("session_id"), "messages": [],
        }

    runner._run_agent = AsyncMock(side_effect=fake_run_agent)
    return runner, captured


def _admin_session(db, session_id="admin-sess"):
    """An existing board-chat session: TUI-owned, not Telegram, not this user's."""
    db.create_session(session_id=session_id, source="tui")
    db.set_session_title(session_id, "Werkbank admin")
    db.append_message(session_id, "user", "board question")
    db.append_message(session_id, "assistant", "board answer")
    return session_id


@pytest.mark.asyncio
async def test_pinned_topic_routes_to_existing_tui_session_without_topic_mode(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    admin = _admin_session(db)
    runner, captured = _runner_with_pin(
        db, {PINNED_THREAD: {"name": "Werkbank", "session_id": admin, "skill": "arxiv"}}, monkeypatch,
    )
    event = _make_event("hello from telegram", thread_id=PINNED_THREAD)
    event.auto_skill = "arxiv"

    result = await runner._handle_message(event)

    assert result == "pinned reply"
    assert captured["session_ids"] == [admin]
    topic_key = build_session_key(_make_source(thread_id=PINNED_THREAD))
    runner.session_store.switch_session.assert_called_once_with(topic_key, admin)
    # No /topic needed: topic mode stays off, yet the binding mirrors the pin.
    assert db.is_telegram_topic_mode_enabled(chat_id=CHAT, user_id=CHAT) is False
    binding = db.get_telegram_topic_binding(chat_id=CHAT, thread_id=PINNED_THREAD)
    assert binding["session_id"] == admin
    assert binding["managed_mode"] == "operator"
    # A topic skill must not be injected into an existing conversation.
    assert event.auto_skill is None
    db.close()


@pytest.mark.asyncio
async def test_pinned_topic_with_unknown_session_refuses_instead_of_new_session(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    runner, captured = _runner_with_pin(
        db, {PINNED_THREAD: {"name": "Werkbank", "session_id": "20260101_missing"}}, monkeypatch,
    )

    result = await runner._handle_message(_make_event("hello", thread_id=PINNED_THREAD))

    assert "20260101_missing" in result
    assert "does not exist in this profile" in result
    runner._run_agent.assert_not_called()
    runner.session_store.switch_session.assert_not_called()
    assert db.get_telegram_topic_binding(chat_id=CHAT, thread_id=PINNED_THREAD) is None
    assert "session_ids" not in captured
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/new", "/reset", "/resume other", "/topic other-session"])
async def test_session_moving_commands_are_refused_in_pinned_topic(tmp_path, monkeypatch, command):
    db = SessionDB(db_path=tmp_path / "state.db")
    admin = _admin_session(db)
    db.create_session(session_id="other-session", source="telegram", user_id=CHAT)
    runner, _captured = _runner_with_pin(
        db, {PINNED_THREAD: {"name": "Werkbank", "session_id": admin}}, monkeypatch,
    )
    await runner._handle_message(_make_event("first", thread_id=PINNED_THREAD))
    runner.session_store.switch_session.reset_mock()

    result = await runner._handle_message(_make_event(command, thread_id=PINNED_THREAD))

    text = getattr(result, "text", None) or getattr(result, "content", None) or str(result)
    assert f"pinned to session `{admin}`" in text
    runner.session_store.reset_session.assert_not_called()
    runner.session_store.switch_session.assert_not_called()
    binding = db.get_telegram_topic_binding(chat_id=CHAT, thread_id=PINNED_THREAD)
    assert binding["session_id"] == admin
    # The pinned session was not ended by the refused command.
    assert db.get_session(admin)["ended_at"] is None
    db.close()


@pytest.mark.asyncio
async def test_pinned_topic_follows_compression_tip_of_configured_session(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="admin-root", source="tui")
    db.end_session("admin-root", end_reason="compression")
    db.create_session(session_id="admin-tip", source="tui", parent_session_id="admin-root")
    runner, captured = _runner_with_pin(
        db, {PINNED_THREAD: {"name": "Werkbank", "session_id": "admin-root"}}, monkeypatch,
    )

    await runner._handle_message(_make_event("after compression", thread_id=PINNED_THREAD))

    assert captured["session_ids"] == ["admin-tip"]
    binding = db.get_telegram_topic_binding(chat_id=CHAT, thread_id=PINNED_THREAD)
    assert binding["session_id"] == "admin-tip"
    db.close()


@pytest.mark.asyncio
async def test_pin_rebinds_when_tip_moves_between_messages(tmp_path, monkeypatch):
    """The board chat compresses the session between two Telegram messages: the second message
    lands on the new tip and the binding row is rewritten (the unique session index would
    otherwise keep the stale id)."""
    db = SessionDB(db_path=tmp_path / "state.db")
    admin = _admin_session(db, "admin-a")
    runner, captured = _runner_with_pin(
        db, {PINNED_THREAD: {"name": "Werkbank", "session_id": admin}}, monkeypatch,
    )
    await runner._handle_message(_make_event("one", thread_id=PINNED_THREAD))

    db.end_session(admin, end_reason="compression")
    db.create_session(session_id="admin-b", source="tui", parent_session_id=admin)
    await runner._handle_message(_make_event("two", thread_id=PINNED_THREAD))

    assert captured["session_ids"] == [admin, "admin-b"]
    binding = db.get_telegram_topic_binding(chat_id=CHAT, thread_id=PINNED_THREAD)
    assert binding["session_id"] == "admin-b"
    db.close()


@pytest.mark.asyncio
async def test_unpinned_operator_topic_keeps_its_own_session(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    _admin_session(db)
    runner, captured = _runner_with_pin(db, {FREE_THREAD: {"name": "Scratch"}}, monkeypatch)

    await runner._handle_message(_make_event("hi", thread_id=FREE_THREAD))

    assert captured["session_ids"] == ["sess-topic"]
    runner.session_store.switch_session.assert_not_called()
    db.close()


@pytest.mark.asyncio
async def test_chat_input_topic_restore_still_rejects_foreign_session(tmp_path, monkeypatch):
    """IDOR guard unchanged: naming the admin session from chat input in an unpinned topic is
    refused — only the operator's config can bind a non-Telegram session."""
    db = SessionDB(db_path=tmp_path / "state.db")
    admin = _admin_session(db)
    db.enable_telegram_topic_mode(chat_id=CHAT, user_id=CHAT)
    runner, _captured = _runner_with_pin(
        db, {PINNED_THREAD: {"name": "Werkbank", "session_id": "someone-else"}}, monkeypatch,
    )

    result = await runner._handle_message(_make_event(f"/topic {admin}", thread_id=FREE_THREAD))

    assert "not a Telegram session" in str(result)
    assert db.get_telegram_topic_binding(chat_id=CHAT, thread_id=FREE_THREAD) is None
    db.close()


def test_pinned_lookup_ignores_root_dm_and_general_topic(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    runner = _make_runner(session_db=db)
    adapter = _PinAdapter({"1": {"name": "General", "session_id": "x"}, PINNED_THREAD: {"session_id": "x"}})
    runner._delivery_adapter_for = lambda _source: adapter

    assert runner._operator_pinned_topic_session_id(_make_source(thread_id=None)) is None
    assert runner._operator_pinned_topic_session_id(_make_source(thread_id="1")) is None
    assert runner._operator_pinned_topic_session_id(_make_source(thread_id=PINNED_THREAD)) == "x"
    # A MagicMock adapter (no real _get_dm_topic_info on its class) never reads as pinned.
    runner._delivery_adapter_for = lambda _source: MagicMock()
    assert runner._operator_pinned_topic_session_id(_make_source(thread_id=PINNED_THREAD)) is None
    db.close()

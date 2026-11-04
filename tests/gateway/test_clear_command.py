"""WB-1426: gateway ``/clear`` empties the context of THIS session (Werkbank chat, Telegram).

The session id stays (admin binding, Telegram topic, chat list keep working — unlike ``/new``).
The history is archived rewind-style (``active = 0``), a snapshot is written first and
``/clear undo`` restores it until the next turn. The write goes the context editor's way
(WB-1319, now ``gateway/context_rewrite``): one lock per ``session_key``, the DB turn-lease fence,
the cached agent evicted, ``session.state`` like after ``/compress``. The stored system prompt is
kept byte for byte and the next turn reuses it although its history is empty.

Every test runs against a REAL SessionDB / SessionStore under ``tmp_path`` (never the live state.db).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.session import AsyncSessionStore, SessionSource, SessionStore
from gateway.slash_commands_session import GatewaySessionCommandsMixin
from hermes_state import SessionDB

PROMPT = "Du bist Hermes.\n\nProjekt: /data/projects/beispiel\nModel: test-model\nProvider: anthropic"


def _source() -> SessionSource:
    return SessionSource(platform=Platform.TELEGRAM, user_id="u1", chat_id="c1", user_name="tester",
                         chat_type="dm")


class _Runner(GatewaySessionCommandsMixin):
    """The mixin's /clear handler with the runner surface it uses — nothing else of the gateway."""

    def __init__(self, store: SessionStore, home: Path):
        self.session_store = store
        self.async_session_store = AsyncSessionStore(store)
        self._session_db = store._db
        self._running_agents = {}
        self._evicted = []
        self._agent = None
        self._home = home
        self.config = SimpleNamespace(multiplex_profiles=False)

    def _session_key_for_source(self, source):
        return self.session_store._generate_session_key(source)

    def _cached_agent_for(self, session_key, lockless_fallback=False):
        return self._agent

    def _evict_cached_agent(self, session_key):
        self._evicted.append(session_key)
        self._agent = None

    def _context_snapshot_home(self, source):
        return self._home


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db = SessionDB(db_path=tmp_path / "state.db")
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    store._db = db
    entry = store.get_or_create_session(_source())
    sid = entry.session_id
    db.update_system_prompt(sid, PROMPT)
    db.append_message(sid, "user", "erste Frage")
    db.append_message(sid, "assistant", "", tool_calls=[{"id": "call_a", "type": "function",
                                                       "function": {"name": "shell", "arguments": "{}"}}])
    db.append_message(sid, "tool", "ausgabe A", tool_call_id="call_a", tool_name="shell")
    db.append_message(sid, "assistant", "Antwort eins")
    db.append_message(sid, "user", "zweite Frage")
    db.append_message(sid, "assistant", "Antwort zwei")
    runner = _Runner(store, tmp_path)
    key = runner._session_key_for_source(_source())
    return SimpleNamespace(db=db, store=store, sid=sid, runner=runner, key=key, home=tmp_path)


def _run(runner, text):
    return asyncio.run(runner._handle_clear_command(MessageEvent(text=text, source=_source(), message_id="m1")))


def _view(db, sid):
    keep = ("role", "content", "tool_calls", "tool_call_id")
    return [{k: m.get(k) for k in keep if m.get(k) not in (None, "")}
            for m in db.get_messages_as_conversation(sid)]


def _snaps(home, sid):
    from gateway import context_rewrite
    return context_rewrite.snapshots(context_rewrite.snapshot_root(home), sid)


# ── 1. leer, gleiche Sitzung, Archiv, Schnappschuss ──────────────────────────────────────────

def test_clear_empties_the_history_of_the_same_session_and_archives_it(env):
    before = _view(env.db, env.sid)
    reply = _run(env.runner, "/clear")
    assert "Kontext geleert (6 Nachrichten archiviert)" in reply
    assert "/clear undo" in reply
    assert "System-Prompt" in reply                         # owner: say what stays
    assert _view(env.db, env.sid) == []                      # the next turn loads nothing
    assert env.store.load_transcript(env.sid) == []
    # same session: the routing entry and the DB row are the old ones
    assert env.store.get_or_create_session(_source()).session_id == env.sid
    assert env.db.get_session(env.sid) is not None
    # archived, not deleted
    archived = env.db.get_messages_as_conversation(env.sid, include_inactive=True)
    assert len(archived) >= len(before)
    assert len(_snaps(env.home, env.sid)) == 1


def test_clear_keeps_the_stored_system_prompt_byte_for_byte(env):
    before = env.db.get_session(env.sid)
    _run(env.runner, "/clear")
    after = env.db.get_session(env.sid)
    assert after["system_prompt"] == before["system_prompt"] == PROMPT
    for field in ("model", "title", "cwd", "profile_name"):
        assert after.get(field) == before.get(field)


def test_clear_evicts_the_cached_agent_of_the_session(env):
    env.runner._agent = SimpleNamespace(provider="anthropic", context_compressor=None)
    _run(env.runner, "/clear")
    assert env.runner._evicted == [env.key]


def test_the_next_turn_reuses_the_stored_prompt_without_the_old_messages(env):
    """What the NEXT turn does after /clear: the gateway marks the agent, the core restores the
    stored prompt although the history is empty (it would rebuild it otherwise)."""
    from unittest.mock import MagicMock

    from agent.conversation_loop import _restore_or_build_system_prompt
    from gateway import context_rewrite

    _run(env.runner, "/clear")
    history = env.store.load_transcript(env.sid)
    assert history == []
    agent = MagicMock()
    agent._cached_system_prompt = None
    agent.session_id = env.sid
    agent.model, agent.provider, agent.platform = "test-model", "anthropic", "telegram"
    agent._session_db = env.db
    agent._use_prompt_caching = False
    agent._build_system_prompt = MagicMock(return_value="NEU GEBAUT")
    agent.enabled_toolsets = agent.disabled_toolsets = None
    agent._restore_stored_system_prompt = context_rewrite.restore_stored_prompt(
        env.store, env.key, env.sid)
    assert agent._restore_stored_system_prompt is True
    _restore_or_build_system_prompt(agent, None, history)
    assert agent._cached_system_prompt == PROMPT
    agent._build_system_prompt.assert_not_called()
    assert env.db.get_session(env.sid)["system_prompt"] == PROMPT


def test_without_a_clear_the_marker_is_off(env):
    from gateway import context_rewrite
    assert context_rewrite.restore_stored_prompt(env.store, env.key, env.sid) is False
    assert context_rewrite.restore_stored_prompt(None, env.key, env.sid) is False


# ── 2. /clear undo ──────────────────────────────────────────────────────────────────────────

def test_clear_undo_restores_the_history_exactly(env):
    before = _view(env.db, env.sid)
    _run(env.runner, "/clear")
    reply = _run(env.runner, "/clear undo")
    assert "wiederhergestellt" in reply and "6" in reply
    assert _view(env.db, env.sid) == before
    assert _snaps(env.home, env.sid) == []                   # the snapshot is used up
    assert env.runner._evicted == [env.key, env.key]
    again = _run(env.runner, "/clear undo")
    assert "Schnappschuss" in again
    assert _view(env.db, env.sid) == before


def test_clear_undo_after_the_next_turn_is_refused(env):
    _run(env.runner, "/clear")
    env.db.append_message(env.sid, "user", "neue Frage")
    env.db.append_message(env.sid, "assistant", "neue Antwort")
    moved = _view(env.db, env.sid)
    reply = _run(env.runner, "/clear undo")
    assert "weitergegangen" in reply
    assert _view(env.db, env.sid) == moved
    assert len(_snaps(env.home, env.sid)) == 1


def test_clear_undo_does_not_take_back_an_editor_snapshot(env):
    from gateway import context_rewrite

    root = context_rewrite.snapshot_root(env.home)
    current = context_rewrite.read_messages(env.db, env.sid)
    context_rewrite.write_snapshot(root, env.sid, "edit", current,
                                   expected_after=context_rewrite.fingerprint(current))
    before = _view(env.db, env.sid)
    reply = _run(env.runner, "/clear undo")
    assert "/clear" in reply and "Schnappschuss" in reply
    assert _view(env.db, env.sid) == before


def test_clear_of_an_empty_context_writes_nothing(env):
    _run(env.runner, "/clear")
    reply = _run(env.runner, "/clear")
    assert "schon leer" in reply
    assert len(_snaps(env.home, env.sid)) == 1


def test_an_unknown_argument_answers_the_usage(env):
    before = _view(env.db, env.sid)
    reply = _run(env.runner, "/clear alles")
    assert "/clear undo" in reply
    assert _view(env.db, env.sid) == before and env.runner._evicted == []


# ── 3. laufender Zug, fremder Schreiber, gemeinsamer Lock ──────────────────────────────────

def test_a_turn_lease_of_another_process_refuses_the_clear(env):
    before = _view(env.db, env.sid)
    assert env.db.try_acquire_session_turn_lease(env.sid, "anderes-fenster:7", ttl_seconds=60)
    reply = _run(env.runner, "/clear")
    assert "Zug" in reply and "/stop" in reply
    assert _view(env.db, env.sid) == before
    assert _snaps(env.home, env.sid) == []                   # the refused clear leaves no snapshot
    assert env.runner._evicted == []
    assert __import__("gateway.context_rewrite").context_rewrite.restore_stored_prompt(
        env.store, env.key, env.sid) is False


def test_a_running_turn_in_this_gateway_refuses_the_clear(env):
    before = _view(env.db, env.sid)
    env.runner._running_agents[env.key] = object()
    reply = _run(env.runner, "/clear")
    assert "/stop" in reply
    assert _view(env.db, env.sid) == before and env.runner._evicted == []


def test_mid_run_dispatch_rejects_clear_with_the_stop_hint(env):
    from gateway.run import GatewayRunner
    from hermes_cli.commands import resolve_command

    cmd = resolve_command("clear")
    assert cmd.busy_handler == "clear"
    assert GatewayRunner._BUSY_SPECIAL_HANDLERS["clear"] == "_busy_clear_command"
    reply = asyncio.run(env.runner._busy_clear_command(
        MessageEvent(text="/clear", source=_source(), message_id="m1"), env.key, _source()))
    assert "/stop" in reply
    assert len(_view(env.db, env.sid)) == 6


def test_the_werkbank_editor_lock_is_the_same_lock(env, monkeypatch):
    """Werkbank context editor and /clear serialize on ONE lock per session_key."""
    from gateway import context_rewrite
    from plugins.platforms.werkbank import kontext

    assert kontext._session_lock(env.key) is context_rewrite.session_lock(env.key)
    monkeypatch.setattr(context_rewrite, "LOCK_WAIT_S", 0.05)
    lock = context_rewrite.session_lock(env.key)
    assert lock.acquire(timeout=1)
    try:
        reply = _run(env.runner, "/clear")
    finally:
        lock.release()
    assert "gerade" in reply
    assert len(_view(env.db, env.sid)) == 6


def test_the_werkbank_editor_sees_the_clear_snapshot(env):
    from gateway import context_rewrite
    from plugins.platforms.werkbank import __init__ as _unused  # noqa: F401  (plugin importable)
    from plugins.platforms.werkbank import _snapshot_root, kontext

    _run(env.runner, "/clear")
    assert _snapshot_root(env.home) == context_rewrite.snapshot_root(env.home)
    assert kontext.counts(_snapshot_root(env.home), env.sid) == 1


# ── 4. Hinweise: Freigaben/Rückfragen, Statuszeile ─────────────────────────────────────────

def test_open_approvals_and_clarify_prompts_expire_with_a_hint(env, monkeypatch):
    import tools.approval as approval
    import tools.clarify_gateway as clarify

    cleared = []
    monkeypatch.setattr(approval, "list_gateway_approvals", lambda key: [{"request_id": "r1"}])
    monkeypatch.setattr(approval, "clear_session", lambda key: cleared.append(("approval", key)))
    monkeypatch.setattr(clarify, "clear_session", lambda key: cleared.append(("clarify", key)) or 1)
    reply = _run(env.runner, "/clear")
    assert ("approval", env.key) in cleared and ("clarify", env.key) in cleared
    assert "verfallen" in reply


def test_clear_emits_session_state_like_compress(env, monkeypatch):
    from gateway import session_events

    seen = []
    monkeypatch.setattr(session_events, "emit",
                        lambda key, sid, platform, type_, id_=None, parent_id=None, payload=None:
                        seen.append((key, sid, type_, payload or {})))
    env.runner._agent = SimpleNamespace(provider="anthropic", context_compressor=SimpleNamespace(
        context_length=200_000, threshold_tokens=160_000))
    _run(env.runner, "/clear")
    states = [s for s in seen if s[2] == "session.state"]
    assert len(states) == 1
    key, sid, _type, payload = states[0]
    assert key == env.key and sid == env.sid
    assert payload["context_source"] == "kompression"
    assert 0 < payload["context_tokens"] < 1000              # ≈ the system prompt alone
    assert payload["context_length"] == 200_000 and payload["provider"] == "anthropic"

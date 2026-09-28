"""Board chat (TUI/dashboard) and a Telegram operator topic drive the SAME session.

Two processes (two ``SessionDB`` handles on one state.db) submit a turn to one session at the
same time. The durable session turn lease serializes them: the second waits, reloads the
transcript the first one wrote, and then runs. Nothing is lost, nothing is answered twice.
"""

from __future__ import annotations

import threading

from hermes_state import SessionDB
from run_agent import AIAgent
from tests.agent.test_cross_process_turn_lease import _agent_with_db


def _surface_agent(db, platform: str) -> AIAgent:
    agent = _agent_with_db(db, session_id="admin-sess", platform=platform)
    agent._last_flushed_db_idx = 0
    agent._flushed_db_message_ids = set()
    agent._flushed_db_message_session_id = None
    agent._db_flush_scan_prefix = None
    agent._pending_cli_user_message = None
    agent._session_persist_lock = None
    agent._persist_user_message_idx = None
    agent._persist_user_message_override = None
    agent._persist_user_message_timestamp = None
    agent._persist_user_message_platform_id = None
    agent._last_persistence_error_cause = None
    agent._session_turn_lease_refresh_interval = 60.0
    return agent


def test_board_and_telegram_turns_on_one_session_are_serialized(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    board_db, gateway_db = SessionDB(path), SessionDB(path)
    board_db.create_session("admin-sess", source="tui")
    board_db.append_message("admin-sess", "user", "earlier board question")
    board_db.append_message("admin-sess", "assistant", "earlier board answer")

    board = _surface_agent(board_db, "tui")
    telegram = _surface_agent(gateway_db, "telegram")

    board_running = threading.Event()
    telegram_waiting = threading.Event()
    telegram.status_callback = lambda kind, text=None: (
        telegram_waiting.set() if text and "waiting for it to finish" in text else None
    )
    seen_history = {}
    concurrent = {"now": 0, "max": 0}
    lock = threading.Lock()

    def fake_run(agent, message, _system, history, *_args, **_kwargs):
        with lock:
            concurrent["now"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["now"])
        try:
            seen_history[agent.platform] = [m.get("content") for m in history]
            if agent is board:
                board_running.set()
                # Hold the lease until the Telegram turn is provably blocked on it.
                assert telegram_waiting.wait(10), "telegram turn never waited for the lease"
            messages = [
                *history,
                {"role": "user", "content": message},
                {"role": "assistant", "content": f"reply to {message}"},
            ]
            assert agent._flush_messages_to_session_db(messages, history) is True
            return {"final_response": f"reply to {message}", "messages": messages, "failed": False}
        finally:
            with lock:
                concurrent["now"] -= 1

    monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)

    def durable(db):
        return db.get_messages_as_conversation("admin-sess", repair_alternation=True, include_row_ids=True)

    results = {}

    def run_board():
        results["tui"] = AIAgent.run_conversation(
            board, "board message", conversation_history=durable(board_db))

    def run_telegram():
        assert board_running.wait(10)
        # Telegram loaded its history BEFORE the board turn flushed — the stale snapshot the
        # lease-wait reload must replace.
        results["telegram"] = AIAgent.run_conversation(
            telegram, "telegram message", conversation_history=durable(gateway_db))

    threads = [threading.Thread(target=run_board), threading.Thread(target=run_telegram)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not any(thread.is_alive() for thread in threads)

    assert concurrent["max"] == 1, "both surfaces ran a turn on the session at the same time"
    assert results["tui"]["final_response"] == "reply to board message"
    assert results["telegram"]["final_response"] == "reply to telegram message"
    # The waiting turn saw the board's turn (reload after admission), not its stale snapshot.
    assert seen_history["telegram"][-2:] == ["board message", "reply to board message"]

    rows = [(m["role"], m["content"]) for m in gateway_db.get_messages("admin-sess")]
    assert rows == [
        ("user", "earlier board question"),
        ("assistant", "earlier board answer"),
        ("user", "board message"),
        ("assistant", "reply to board message"),
        ("user", "telegram message"),
        ("assistant", "reply to telegram message"),
    ]
    # Both leases were released.
    assert board_db.try_acquire_session_turn_lease("admin-sess", "pid=1:probe", ttl_seconds=5)
    board_db.release_session_turn_lease("admin-sess", "pid=1:probe")
    board_db.close()
    gateway_db.close()

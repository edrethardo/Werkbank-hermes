"""A background delegate's terminal guard must reach the originating chat.

Live report: an async (background) delegate child ran a dangerous ``terminal``
command after the parent Telegram turn had returned; the tool waited the full
approval timeout and the user reported no card. These tests drive the REAL
production hops — ``dispatch_async_delegation`` (notify lease), the
``await_child`` worker (subagent approval callback via pool initializer +
copied Context), the sequential tool executor's extra worker hop and the real
``terminal_tool`` guard — after the parent turn unregistered its notifier.

Nothing is executed: the dangerous command is only a string for
``detect_dangerous_command``; environment setup is stubbed and execution fails
the test if it is ever reached.
"""
from __future__ import annotations

import contextvars
import json
import logging
import threading
import time

import pytest

from tools import approval as A
from tools import approval_context

KEY = "agent:main:telegram:dm:test-child-terminal:51"
DANGEROUS = "rm -rf /tmp/hermes-child-terminal-approval-probe-never-created"


@pytest.fixture
def gateway_turn(monkeypatch):
    """Gateway process state as ``start_gateway`` + a Telegram turn bind it."""
    from gateway.session_context import clear_session_vars, set_session_vars

    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 2)
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)

    def no_cli_prompt(*_a, **_k):
        raise AssertionError("CLI input() fallback reached from a gateway child")

    monkeypatch.setattr("tools.approval_prompt._read_choice", no_cli_prompt)

    def scrub():
        with A._lock:
            for table in (A._gateway_queues, A._gateway_notify_cbs, A._gateway_notify_leases,
                          A._session_approved):
                table.pop(KEY, None)

    scrub()
    tokens = set_session_vars(platform="telegram", source="telegram", chat_id="1", session_key=KEY)
    key_token = approval_context.set_current_session_key(KEY)
    try:
        yield
    finally:
        approval_context.reset_current_session_key(key_token)
        clear_session_vars(tokens)
        scrub()


def _child_terminal_call(monkeypatch):
    """Run the real ``terminal`` tool the way a delegate child does: on the
    ``await_child`` worker (subagent approval callback installed by the pool
    initializer, parent Context copied) and through the sequential tool
    executor's own worker-thread hop."""
    from tools import terminal_tool as TT
    from tools.daemon_pool import DaemonThreadPoolExecutor
    from tools.delegate_tool import _get_subagent_approval_callback, _set_subagent_approval_cb
    from tools.thread_context import propagate_context_to_thread

    def never_run(*_a, **_k):
        raise AssertionError("terminal backend reached: the dangerous command would have run")

    # No real environment, no pre-exec lifecycle probe, and execution fails the test;
    # the approval guard in between (``_run_approval_guards``) stays real.
    monkeypatch.setattr(TT, "_acquire_env", lambda *_a, **_k: object())
    monkeypatch.setattr(TT, "_pre_exec_block", lambda *_a, **_k: None)
    monkeypatch.setattr(TT, "_run_foreground", never_run)
    monkeypatch.setattr(TT, "spawn_background_process", never_run)

    def tool_hop():
        # agent.tool_executor._run_sequential_tool_execution_middleware: one more
        # DaemonThreadPoolExecutor worker wrapped in propagate_context_to_thread.
        hop = DaemonThreadPoolExecutor(max_workers=1)
        try:
            return hop.submit(propagate_context_to_thread(
                lambda: TT.terminal_tool(command=DANGEROUS))).result(timeout=30)
        finally:
            hop.shutdown(wait=False)

    child_pool = DaemonThreadPoolExecutor(
        max_workers=1, initializer=_set_subagent_approval_cb,
        initargs=(_get_subagent_approval_callback(),))
    try:
        return child_pool.submit(contextvars.copy_context().run, tool_hop).result(timeout=30)
    finally:
        child_pool.shutdown(wait=False)


def _run_detached_child(monkeypatch, notify):
    """Parent turn registers ``notify``, dispatches the child, then returns
    (unregisters) BEFORE the child reaches its terminal guard."""
    from tools import async_delegation as ad

    parent_done = threading.Event()
    outcome = {}

    def runner():
        assert parent_done.wait(10)
        outcome["result"] = json.loads(_child_terminal_call(monkeypatch))
        return {"status": "completed", "summary": "test only"}

    ad._reset_for_tests()
    A.register_gateway_notify(KEY, notify)  # parent turn running
    try:
        handle = ad.dispatch_async_delegation(
            goal="test", context="", toolsets=[], role="leaf", model=None,
            session_key=KEY, runner=runner, max_async_children=10)
        assert handle["status"] == "dispatched"
        A.unregister_gateway_notify(KEY)  # parent turn returned
        parent_done.set()
        deadline = time.monotonic() + 20
        while "result" not in outcome and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        parent_done.set()
        A.unregister_gateway_notify(KEY)
        ad._reset_for_tests()
    return outcome["result"]


def test_background_child_terminal_after_parent_turn_end_notifies_once(gateway_turn, monkeypatch, caplog):
    sent = []

    def notify(data):
        sent.append(dict(data))
        # Another chat's answer must not resolve this request; the exact id must.
        assert A.resolve_gateway_approval("telegram:another-chat", "once") == 0
        assert A.resolve_gateway_approval(KEY, "deny", request_id=data["request_id"]) == 1

    with caplog.at_level(logging.INFO, logger="tools.approval"):
        result = _run_detached_child(monkeypatch, notify)

    assert len(sent) == 1, (sent, result)
    request_id = sent[0]["request_id"]
    assert request_id and sent[0]["command"] == DANGEROUS
    assert result.get("status") == "blocked", result
    assert "denied by user" in result["error"], result
    # Delivery is provable from the log: the exact request reached the notifier.
    assert any(f"Approval request {request_id} handed to notifier for session {KEY}" in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]


def test_unanswered_background_child_request_is_logged_and_fails_closed(gateway_turn, monkeypatch, caplog):
    sent = []
    with caplog.at_level(logging.INFO, logger="tools.approval"):
        result = _run_detached_child(monkeypatch, lambda data: sent.append(dict(data)))

    assert len(sent) == 1
    request_id = sent[0]["request_id"]
    assert result.get("status") == "blocked", result
    assert "timed out without user response" in result["error"], result
    messages = [r.getMessage() for r in caplog.records]
    assert any(f"Approval request {request_id} handed to notifier" in m for m in messages), messages
    assert any(f"Approval request {request_id} for session {KEY} ended unanswered (timeout)" in m
               for m in messages), messages

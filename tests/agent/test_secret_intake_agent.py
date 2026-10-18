"""WB-1212 end to end on the agent side: a secret in the user message never reaches the model
request, the SessionDB, a plugin hook or the log — only the placeholder does, and the rest of the
text is byte-identical. ``run_conversation`` is the backstop for every host (CLI, TUI, ACP, API,
cron) that has no earlier intake point. The provider is an in-process mock: no external call.

All secrets are FAKE and assembled at runtime."""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import pytest

from agent import secret_intake as si
from hermes_state import SessionDB

FAKE_GH = "ghp" + "_" + "FAKE0fake1FAKE2fake3FAKE4fake5FAKE6f"
FAKE_PW = "Fake" + "Passw0rd!"


class _Mock(BaseHTTPRequestHandler):
    requests: list = []

    def do_POST(self):  # noqa: N802
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
        type(self).requests.append(req)
        body = {"id": "m", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                        "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        if req.get("stream") is True:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for c in ({"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}]},
                      {"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}):
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a, **kw):
        pass


@pytest.fixture()
def agent_env(tmp_path, monkeypatch):
    si.store().clear()
    monkeypatch.setattr(si, "enabled", lambda: True)
    _Mock.requests = []
    srv = HTTPServer(("127.0.0.1", 0), _Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    db = SessionDB(db_path=tmp_path / "state.db")
    hook_calls = []

    def hook(name, **kw):
        hook_calls.append((name, json.dumps(kw, default=str)))
        return []

    from run_agent import AIAgent

    def make():
        agent = AIAgent(api_key="test-key", base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1",
                        provider="openai-compat", model="test-model", max_iterations=3, enabled_toolsets=[],
                        quiet_mode=True, skip_context_files=True, skip_memory=True, save_trajectories=False,
                        platform="cli", session_db=db, session_id="sess-secret")
        agent.valid_tool_names = set()
        return agent

    try:
        with patch("hermes_cli.plugins.invoke_hook", side_effect=hook):
            yield make, db, hook_calls
    finally:
        srv.shutdown()
        db.close()
        si.store().clear()


def _wire_text() -> str:
    return json.dumps([r for r in _Mock.requests if "messages" in r])


def test_model_request_db_hooks_and_log_hold_only_the_placeholder(agent_env, caplog):
    caplog.set_level(logging.DEBUG)
    make, db, hooks = agent_env
    text = f"Bitte nutze {FAKE_GH} und password={FAKE_PW} für den Deploy.\n  Danke!"
    make().run_conversation(text, conversation_history=[], task_id="t")
    want = "Bitte nutze [GEHEIMNIS:github-token-1] und password=[GEHEIMNIS:passwort-1] für den Deploy.\n  Danke!"

    reqs = [r for r in _Mock.requests if "messages" in r]
    assert reqs, "no model request captured"
    users = [m for m in reqs[0]["messages"] if m.get("role") == "user"]
    assert users[0]["content"].startswith(want)
    wire = _wire_text()
    assert FAKE_GH not in wire and FAKE_PW not in wire

    rows = [r for r in db.get_messages("sess-secret") if r["role"] == "user"]
    assert rows and rows[0]["content"] == want
    dump = json.dumps(db.get_messages("sess-secret"), default=str)
    assert FAKE_GH not in dump and FAKE_PW not in dump

    assert hooks and all(FAKE_GH not in kw and FAKE_PW not in kw for _, kw in hooks)
    assert FAKE_GH not in caplog.text and FAKE_PW not in caplog.text


def test_gateway_persist_override_is_redacted_too(agent_env):
    make, db, _ = agent_env
    make().run_conversation(f"[ts] key {FAKE_GH}", conversation_history=[], task_id="t",
                            persist_user_message=f"key {FAKE_GH}")
    dump = json.dumps(db.get_messages("sess-secret"), default=str)
    assert FAKE_GH not in dump and "[GEHEIMNIS:github-token-1]" in dump
    assert FAKE_GH not in _wire_text()


def test_switch_off_sends_text_unchanged(agent_env, monkeypatch):
    monkeypatch.setattr(si, "enabled", lambda: False)
    make, db, _ = agent_env
    make().run_conversation(f"key {FAKE_GH}", conversation_history=[], task_id="t")
    assert FAKE_GH in _wire_text()


# -- steer / redirect: every text into the running turn goes through the intake (WB-1212) -------

def _bare_steer_agent(session_id="sess-steer"):
    from run_agent import AIAgent
    agent = object.__new__(AIAgent)
    agent.session_id = session_id
    agent.platform = "cli"
    agent._pending_steer = None
    agent._pending_steer_lock = threading.Lock()
    agent._pending_redirect = None
    agent._pending_redirect_lock = threading.Lock()
    agent._model_request_active = threading.Event()
    agent._executing_tools = False
    agent._execution_thread_id = None
    agent._interrupt_thread_signal_pending = False
    agent._interrupt_requested = False
    agent._interrupt_message = None
    agent._active_children = []
    agent._active_children_lock = threading.Lock()
    agent._tool_worker_threads = None
    agent._tool_worker_threads_lock = None
    return agent


@pytest.fixture()
def intake_on(monkeypatch):
    si.store().clear()
    monkeypatch.setattr(si, "enabled", lambda: True)
    yield
    si.store().clear()


def test_steer_slot_holds_only_the_placeholder(intake_on):
    agent = _bare_steer_agent()
    assert agent.steer(f"nimm {FAKE_GH} bitte") is True
    assert agent._pending_steer == "nimm [GEHEIMNIS:github-token-1] bitte"
    assert si.store().take_unoffered("sess-steer", agent._pending_steer)  # value waits for the vault


def test_steer_folded_parts_are_redacted_too(intake_on):
    from agent.folded_messages import FoldedText, folded_parts
    agent = _bare_steer_agent()
    agent.steer(FoldedText(f"[Alice] key {FAKE_GH}", [{"ref": "r1", "text": f"key {FAKE_GH}"}]))
    assert FAKE_GH not in agent._pending_steer
    parts = folded_parts(agent._pending_steer)
    assert parts == [{"ref": "r1", "text": "key [GEHEIMNIS:github-token-1]"}]


def test_redirect_during_model_request_is_redacted(intake_on):
    agent = _bare_steer_agent()
    agent._model_request_active.set()
    assert agent.redirect(f"password={FAKE_PW} statt dessen") is True
    assert agent._pending_redirect == "password=[GEHEIMNIS:passwort-1] statt dessen"


def test_redirect_during_tools_degrades_to_a_redacted_steer(intake_on):
    agent = _bare_steer_agent()
    agent._executing_tools = True
    assert agent.redirect(f"x {FAKE_GH}") is True
    assert agent._pending_steer == "x [GEHEIMNIS:github-token-1]"


def test_redirect_native_codex_steer_gets_the_placeholder(intake_on):
    agent = _bare_steer_agent()
    seen = []
    agent._codex_session = None
    with patch("agent.interrupt_control._ic_codex_method",
               return_value=lambda text: seen.append(text) or True):
        assert agent.redirect(f"y {FAKE_GH}") is True
    assert seen == ["y [GEHEIMNIS:github-token-1]"]


def test_steer_switch_off_keeps_text(monkeypatch):
    monkeypatch.setattr(si, "enabled", lambda: False)
    agent = _bare_steer_agent()
    agent.steer(f"k {FAKE_GH}")
    assert agent._pending_steer == f"k {FAKE_GH}"

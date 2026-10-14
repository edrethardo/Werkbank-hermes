"""WB-1128 interop: the Werkbank side talks to the REAL plugin server.

* the board's bridge client ``bruecke.py`` (Werkbank repo, imported by path; never copied):
  key and port from the profile home, resolve, snapshot, events with ``after``, send, stop;
* the board's chat view ``hermes_view.js`` (WB-1132) under node: a real plugin snapshot plus the
  following stream events through the view's own reducer — every field the view reads is there,
  and a Werkbank message is ONE bubble (snapshot id == stream id).

Paths: ``WERKBANK_REPO`` (default ``/data/projects/agent_ticket``); the view from
``WERKBANK_VIEW_JS`` or the first existing of the WB-1132 worktree and the repo. Missing → skip.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from gateway import session_events
from gateway.platforms.event import MessageEvent
from test_werkbank_gateway import (Ctx127, FakeOwner, _injected_event, _source, _subscribe, PLUGIN_ID,
                                   _clean_bus)  # noqa: F401  (autouse fixture)
from werkbank_testkit import KEY_SESSION, SID

REPO = Path(os.environ.get("WERKBANK_REPO", "/data/projects/agent_ticket"))
BRUECKE = REPO / "src" / "werkbank" / "plugins" / "chat" / "bruecke.py"
VIEW_CANDIDATES = [Path(p) for p in (os.environ.get("WERKBANK_VIEW_JS", ""),
                                      str(REPO / ".worktrees" / "wb-1132" / "src/werkbank/plugins/chat/hermes_view.js"),
                                      str(REPO / "src/werkbank/plugins/chat/hermes_view.js")) if p]


def _bruecke():
    if not BRUECKE.is_file():
        pytest.skip(f"Werkbank bridge not found at {BRUECKE}")
    spec = importlib.util.spec_from_file_location("werkbank_bruecke_interop", BRUECKE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(tmp_path, monkeypatch):
    """The plugin as the gateway starts it (``plugins.platforms.werkbank.start``) in a profile home
    whose config.yaml carries the port the board reads too."""
    import plugins.platforms.werkbank as plugin
    from hermes_state import SessionDB
    from plugins.platforms.werkbank import config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    port = _free_port()
    (tmp_path / "config.yaml").write_text(
        f"plugins:\n  entries:\n    werkbank:\n      enabled: true\n      port: {port}\n"
        f"      mirror_to_telegram: true\n")
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id=SID, source="telegram")
    owner = FakeOwner(db)
    ctx = Ctx127(owner)
    ctx._manager.home_path = tmp_path
    settings = config.read(tmp_path)
    assert settings.enabled and settings.port == port
    srv = plugin.start(ctx, tmp_path, settings)
    assert srv is not None and srv.port == port
    _subscribe(srv.buffers)
    try:
        yield type("Live", (), {"srv": srv, "db": db, "owner": owner, "ctx": ctx, "home": tmp_path,
                                "port": port})
    finally:
        srv.stop()
        owner.close()


def _emit_deltas(n0, n):
    scope = session_events.TurnScope(session_key=KEY_SESSION, session_id=SID, platform="telegram",
                                     turn_id="turn_x")
    for i in range(n0, n0 + n):
        scope.emit("text.delta", None, "turn_x", {"text": f"t{i}"})


def _collect(stream, count, timeout=5.0):
    out = []
    deadline = time.monotonic() + timeout
    try:
        for item in stream:
            if item is not None:
                out.append(item)
            if len(out) >= count or time.monotonic() > deadline:
                break
    finally:
        stream.close()
    return out


def test_bridge_speaks_with_the_real_plugin_server(live):
    bruecke = _bruecke()
    client = bruecke.Hermes.for_profile("default", live.home, board_port=8765)
    assert client.port == live.port

    key = client.resolve(SID)
    assert key == KEY_SESSION
    status, snap = client.call("GET", client.session_path(key, "snapshot"))
    assert status == 200 and snap["seq"] == 0 and snap["state"] == "wartet"

    _emit_deltas(1, 5)
    first = _collect(client.events(key, snap["epoch"], 0), 3)
    assert [e["seq"] for e in first] == [1, 2, 3]                     # client hangs up after three
    _emit_deltas(6, 2)
    rest = _collect(client.events(key, snap["epoch"], first[-1]["seq"]), 4)
    assert [e["seq"] for e in rest] == [4, 5, 6, 7]                    # exactly the missing ones
    assert [e["payload"]["text"] for e in first + rest] == [f"t{i}" for i in range(1, 8)]

    stale = _collect(client.events(key, snap["epoch"] - 1, 0), 2, timeout=2.0)
    assert [e["type"] for e in stale] == ["session.epoch"]            # carries session_key: passed on

    status, answer = client.call("POST", client.session_path(key, "messages"),
                                 {"text": "Bitte prüfen", "client_message_id": "wb-1"})
    assert (status, answer) == (200, {"accepted": True})
    assert live.ctx.injected[-1] == {"text": "Bitte prüfen", "session_key": KEY_SESSION,
                                     "client_message_id": "wb-1"}
    status, answer = client.call("POST", client.session_path(key, "messages"),
                                 {"text": "Bitte prüfen", "client_message_id": "wb-1"})
    assert status == 409 and isinstance(answer.get("grund"), str)

    assert client.call("POST", client.session_path(key, "stop"), {}) == (200, {"stopped": True})
    status, _ = client.call("POST", client.session_path(key, "approvals", "req-1"), {"choice": "once"})
    assert status == 404                     # WB-1129: nothing waits on req-1 in this session
    from tools import approval
    from tools.approval_gateway_wait import _ApprovalEntry
    waiting = _ApprovalEntry({"request_id": "req-2", "command": "rm -rf /tmp/x"})
    with approval._lock:
        approval._gateway_queues.setdefault(KEY_SESSION, []).append(waiting)
    try:
        status, answer = client.call("POST", client.session_path(key, "approvals", "req-2"), {"choice": "once"})
        assert (status, answer["resolved"]) == (200, 1)      # the shape bruecke/chat.py freigabe reads
        assert waiting.result == "once" and waiting.decided_by == "werkbank"
    finally:
        with approval._lock:
            approval._gateway_queues.pop(KEY_SESSION, None)

    wrong = bruecke.Hermes(live.port, "falsch")
    with pytest.raises(bruecke.HermesAus):
        wrong.resolve(SID)
    deadline = time.monotonic() + 3
    while not live.owner.adapter.sent and time.monotonic() < deadline:
        time.sleep(0.02)
    content = live.owner.adapter.sent[0][1]
    assert content.startswith("👤 *") and content.endswith(" · Werkbank*\n>Bitte prüfen")   # WB-1148


# ── the chat view (hermes_view.js, WB-1132) reads a real snapshot ──────────────────────────

NODE_RUN = r"""
const fs = require("fs");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const st = hvState();
hvSnapshot(st, input.snapshot);
hvConnected(st);
const after = input.events.map(ev => hvEvent(st, ev));
const items = st.order.map(k => st.byKey[k]);
const flat = [];
const walk = (item) => { flat.push(item); for (const p of item.parts || []) if (p.t === "ref") walk(st.byKey[p.key]); };
items.forEach(walk);
console.log(JSON.stringify({
  phase: st.phase, epoch: st.epoch, seq: st.seq, turn: st.turn,
  session: st.session, after: after,
  users: flat.filter(i => i.kind === "user").map(i => ({id: i.id, text: i.text, origin: i.origin})),
  answers: flat.filter(i => i.kind === "answer").map(i => i.parts.filter(p => p.t === "text").map(p => p.text).join("")),
  tools: flat.filter(i => i.kind === "tool").map(i => ({id: i.id, name: i.name, status: i.status, truncated: i.truncated})),
  approvals: flat.filter(i => i.kind === "approval").map(i => ({id: i.id, command: i.command, allowAlways: i.allowAlways})),
  clarify: flat.filter(i => i.kind === "clarify").map(i => ({id: i.id, question: i.question, choices: i.choices})),
}));
"""


def _view():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    for path in VIEW_CANDIDATES:
        if path.is_file() and "hvSnapshot" in path.read_text(encoding="utf-8"):
            return node, path
    pytest.skip("hermes_view.js (WB-1132) not found")


def _run_view(node, view, snapshot, events, tmp_path):
    code = view.read_text(encoding="utf-8").replace("export function", "function").replace(
        "export const", "const")
    script = tmp_path / "view_run.js"
    script.write_text("globalThis.document = globalThis.document || {};\n" + code + "\n" + NODE_RUN,
                      encoding="utf-8")
    proc = subprocess.run([node, str(script)], input=json.dumps({"snapshot": snapshot, "events": events}),
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_chat_view_reads_a_real_plugin_snapshot_and_dedupes_by_the_same_id(live, tmp_path, monkeypatch):
    import tools.approval as approval
    from tools import clarify_gateway

    node, view = _view()
    bruecke = _bruecke()
    client = bruecke.Hermes.for_profile("default", live.home)
    key = client.resolve(SID)

    text = "Bitte die Tests laufen lassen"
    assert client.call("POST", client.session_path(key, "messages"),
                       {"text": text, "client_message_id": "wb-42"})[1] == {"accepted": True}
    live.db.append_message(SID, "user", text,                      # the gateway persisted the turn
                           display_metadata={"client_message_id": "wb-42"})   # with its id on the row
    live.db.append_message(SID, "user", "und aus Telegram", platform_message_id="tg-7")
    live.db.append_message(SID, "assistant", None, tool_calls=[{"id": "call_1", "type": "function",
                                                                 "function": {"name": "terminal"}}])
    live.db.append_message(SID, "tool", json.dumps({"output": "x" * 6000, "exit_code": 1}), tool_call_id="call_1",
                           tool_name="terminal")
    live.db.append_message(SID, "assistant", "Tests laufen.")
    monkeypatch.setattr(approval, "list_gateway_approvals", lambda k: [{
        "request_id": "req-1", "command": "rm -rf build", "description": "delete",
        "allow_permanent": True}])
    entry = clarify_gateway.register("clar-1", KEY_SESSION, "Welche Variante?", ["A", "B"])
    assert session_events.wait_idle(5)              # its clarify.request is in the buffer before the snapshot
    try:
        status, snap = client.call("GET", client.session_path(key, "snapshot"))
        assert status == 200
        # The stream then announces both messages (the board key must not change → no 2nd bubble).
        session_events.user_message(KEY_SESSION, _injected_event(live.ctx.injected[-1]), "turn")
        session_events.user_message(KEY_SESSION, MessageEvent(text="und aus Telegram", message_id="tg-7",
                                                              source=_source(), metadata={}),
                                    "turn", session_id=SID)
        events = _collect(client.events(key, snap["epoch"], snap["seq"]), 2)
        assert [e["type"] for e in events] == ["user.message", "user.message"]
    finally:
        clarify_gateway.resolve_gateway_clarify(entry.clarify_id, "A")

    out = _run_view(node, view, snap, events, tmp_path)
    assert out["phase"] == "live" and out["epoch"] == snap["epoch"]
    assert [a["applied"] for a in out["after"]] == [True, True]
    assert out["users"] == [{"id": "wb-42", "text": text, "origin": "werkbank"},
                            {"id": "tg-7", "text": "und aus Telegram", "origin": "telegram"}]
    assert out["tools"] == [{"id": "call_1", "name": "terminal", "status": "err", "truncated": True}]
    assert out["answers"][-1] == "Tests laufen."
    assert out["approvals"] == [{"id": "req-1", "command": "rm -rf build", "allowAlways": True}]
    assert out["clarify"] == [{"id": "clar-1", "question": "Welche Variante?", "choices": ["A", "B"]}]
    assert out["session"]["tools"] == 0 and out["session"]["errors"] == 0


def test_chat_view_resync_after_turn_done_shows_the_answer_once(live, tmp_path):
    """The view re-syncs once after ``turn.done``; the answer row may not be in the SessionDB yet."""
    node, view = _view()
    bruecke = _bruecke()
    client = bruecke.Hermes.for_profile("default", live.home)
    key = client.resolve(SID)
    status, before = client.call("GET", client.session_path(key, "snapshot"))
    scope = session_events.TurnScope(session_key=KEY_SESSION, session_id=SID, platform="telegram",
                                     turn_id="turn_z")
    scope.emit("turn.start", "turn_z", None, {})
    scope.emit("text.delta", None, "turn_z", {"text": "Alles grün."})
    session_events.end_turn(scope, result={"final_response": "Alles grün.", "completed": True})
    events = _collect(client.events(key, before["epoch"], before["seq"]), 3)
    assert [e["type"] for e in events] == ["turn.start", "text.delta", "turn.done"]
    status, after = client.call("GET", client.session_path(key, "snapshot"))   # no DB row yet
    out = _run_view(node, view, after, [], tmp_path)
    assert out["answers"] == ["Alles grün."] and out["seq"] == after["seq"] == before["seq"] + 3
    live.db.append_message(SID, "assistant", "Alles grün.")                  # the row lands later
    status, later = client.call("GET", client.session_path(key, "snapshot"))
    assert _run_view(node, view, later, [], tmp_path)["answers"] == ["Alles grün."]

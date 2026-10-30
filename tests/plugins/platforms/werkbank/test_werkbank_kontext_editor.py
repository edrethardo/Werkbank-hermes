"""WB-1319: the context editor of a chat session — inspect, edit (with a snapshot and undo) and
compress with an instruction, over the protocol of ``PROTOCOL.md``.

The gateway itself is faked (``FakeHost``); the editor's own logic (order, tokens, tool pairs,
snapshot, undo, gates) is the code under test. ``HostGlue`` below proves the other half: that the
production host hands the right things to the SessionDB and to the core's compression.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from werkbank_testkit import KEY_SESSION, SID, FakeHost, call, core_event, running_server, session_path

SECRET = "sk-ant-api03-" + "A" * 30          # a provider key the display must never show


def _row(rid: int, role: str, content: Any, **extra) -> Dict[str, Any]:
    """One transcript row as ``get_messages_as_conversation(include_row_ids=True)`` hands it over."""
    row = {"role": role, "content": content, "_row_id": rid, "_db_persisted": True,
           "timestamp": 1_790_000_000 + rid}
    row.update(extra)
    return row


def _base(text: str) -> str:
    """What the board sends as ``base``: the sha256 of the WHOLE text it edited."""
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _call(call_id: str, name: str = "shell") -> Dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


def _transcript() -> List[Dict[str, Any]]:
    """user, assistant+tool call, its result, the answer — with one tool pair."""
    return [
        _row(1, "user", "erste Frage"),
        _row(2, "assistant", "", tool_calls=[_call("call_a")]),
        _row(3, "tool", "ausgabe A", tool_call_id="call_a", tool_name="shell"),
        _row(4, "assistant", "Antwort eins"),
        _row(5, "user", "zweite Frage"),
        _row(6, "assistant", "Antwort zwei"),
    ]


def _server(tmp_path, conversation=None, **kw):
    host = FakeHost()
    host.conversation = _transcript() if conversation is None else conversation
    host.system_prompt = "Du bist Hermes.\n\nProjekt: agent_ticket"
    srv = running_server(tmp_path, host, **kw)
    srv.buffers.watch(KEY_SESSION, SID, "telegram")
    return srv


def _numbers(srv, tokens=84_000, length=200_000):
    """What a turn end tells the status line (WB-1309)."""
    srv.buffers.on_event(core_event(1, "turn.done", {"final_response": "ok", "model": "m",
                                                     "context_tokens": tokens,
                                                     "context_length": length,
                                                     "compression_threshold": 160_000}, id_="t1"))


def _inspect(srv, path="context"):
    status, answer = call(srv.port, "GET", session_path(path), srv.key)
    assert status == 200, answer
    return answer


def _post(srv, path, body):
    return call(srv.port, "POST", session_path("context", path), srv.key, body)


# ── 1. Inspizieren: Reihenfolge, Tokens wie die Statuszeile, geschwärzt ─────────────────────

def test_list_is_in_order_and_carries_the_status_line_numbers(tmp_path):
    srv = _server(tmp_path)
    try:
        _numbers(srv)
        out = _inspect(srv)
        assert [i["role"] for i in out["items"]] == ["user", "assistant", "tool", "assistant",
                                                     "user", "assistant"]
        assert [i["preview"] for i in out["items"]][:2] == ["erste Frage", "shell"]
        # the numbers ARE the status line's: same buffer, same fields
        _, snap = call(srv.port, "GET", session_path("snapshot"), srv.key)
        assert out["context"]["tokens"] == snap["session"]["context_tokens"] == 84_000
        assert out["context"]["length"] == 200_000
        assert out["context"]["percent"] == 42
        assert out["context"]["threshold"] == 160_000
        # per item: an estimate of the same counting, with its share of the window
        assert all(i["tokens"] > 0 for i in out["items"])
        assert out["estimate"]["items"] == sum(i["tokens"] for i in out["items"])
        assert out["items"][0]["share"] == pytest.approx(out["items"][0]["tokens"] / 200_000, abs=1e-4)
        assert out["state"] == "wartet" and out["editable"] is True and out["grund"] == ""
    finally:
        srv.server.stop()


def test_system_prompt_is_shown_as_sections_and_never_editable(tmp_path):
    srv = _server(tmp_path)
    try:
        out = _inspect(srv)
        assert out["system"]["bekannt"] is True
        assert "Du bist Hermes." in out["system"]["sections"][0]["text"]
        assert out["system"]["editable"] is False
        assert out["items"][0]["editable"] == "text"           # user text
        assert out["items"][1]["editable"] == "text"           # assistant text
        assert out["items"][2]["editable"] == "shorten"        # a tool output: shorten or remove
        assert out["items"][1]["pair"] == out["items"][2]["pair"] == "call_a"
    finally:
        srv.server.stop()


def test_long_texts_are_cut_in_the_list_and_whole_behind_the_text_route(tmp_path):
    long_text = SECRET + " " + "x" * 30_000
    masked = "sk-ant...AAAA"          # the gateway's own display mask for a provider key
    srv = _server(tmp_path, conversation=[_row(1, "user", long_text)])
    try:
        out = _inspect(srv)
        item = out["items"][0]
        assert item["truncated"] is True and len(item["text"]) <= 9000
        assert SECRET not in item["text"] and masked in item["text"]
        status, full = call(srv.port, "GET", session_path("context", "text", "0"), srv.key)
        assert status == 200 and len(full["text"]) > 20_000
        assert SECRET not in full["text"] and masked in full["text"]
        status, bad = call(srv.port, "GET", session_path("context", "text", "99"), srv.key)
        assert status == 404 and bad["error"]
    finally:
        srv.server.stop()


# ── 2. Bearbeiten: die Historie des nächsten Zugs ändert sich ───────────────────────────────

def test_editing_a_message_changes_the_history_the_next_turn_sees(tmp_path):
    srv = _server(tmp_path)
    try:
        status, full = call(srv.port, "GET", session_path("context", "text", "0"), srv.key)
        assert status == 200 and full["editable"] is True and full["base"] == _base("erste Frage")
        status, out = _post(srv, "edit", {"ops": [{"op": "edit", "id": 1, "text": "bessere Frage",
                                                   "base": full["base"]}]})
        assert status == 200, out
        assert out["ok"] is True and out["changed"] == 1 and out["snapshot"]
        assert srv.host.conversation[0]["content"] == "bessere Frage"
        # nothing else moved, and the tool pair is untouched
        assert [m["role"] for m in srv.host.conversation] == ["user", "assistant", "tool",
                                                              "assistant", "user", "assistant"]
        assert srv.host.conversation[1]["tool_calls"][0]["id"] == "call_a"
        assert "Prompt-Cache" in out["hinweis"] or "prompt-cache" in out["hinweis"].lower()
        after = _inspect(srv)
        assert after["items"][0]["preview"] == "bessere Frage"
        assert after["undo"] == 1                                    # one snapshot to undo
    finally:
        srv.server.stop()


def test_removing_a_tool_result_takes_its_call_with_it(tmp_path):
    srv = _server(tmp_path)
    try:
        status, out = _post(srv, "edit", {"ops": [{"op": "remove", "id": 3}]})
        assert status == 200, out
        rows = srv.host.conversation
        assert [m["role"] for m in rows] == ["user", "assistant", "user", "assistant"]
        assert all(m.get("tool_call_id") != "call_a" for m in rows)
        assert all(m.get("tool_calls") in (None, []) for m in rows)
        assert out["removed"] == 2                                   # the pair, never half
    finally:
        srv.server.stop()


def test_removing_an_assistant_call_takes_its_result_with_it(tmp_path):
    srv = _server(tmp_path)
    try:
        status, out = _post(srv, "edit", {"ops": [{"op": "remove", "id": 2}]})
        assert status == 200, out
        assert [m["role"] for m in srv.host.conversation] == ["user", "assistant", "user", "assistant"]
        assert all(m.get("tool_call_id") != "call_a" for m in srv.host.conversation)
        assert out["removed"] == 2
    finally:
        srv.server.stop()


def test_a_call_beside_another_one_keeps_the_row(tmp_path):
    conversation = [
        _row(1, "user", "frage"),
        _row(2, "assistant", "ich prüfe", tool_calls=[_call("call_a"), _call("call_b", "read")]),
        _row(3, "tool", "ausgabe A", tool_call_id="call_a", tool_name="shell"),
        _row(4, "tool", "ausgabe B", tool_call_id="call_b", tool_name="read"),
        _row(5, "assistant", "fertig"),
    ]
    srv = _server(tmp_path, conversation=conversation)
    try:
        status, out = _post(srv, "edit", {"ops": [{"op": "remove", "id": 3}]})
        assert status == 200, out
        rows = srv.host.conversation
        assert [m["role"] for m in rows] == ["user", "assistant", "tool", "assistant"]
        assert [c["id"] for c in rows[1]["tool_calls"]] == ["call_b"]
        assert rows[1]["content"] == "ich prüfe"                     # text stays
        assert rows[2]["tool_call_id"] == "call_b"
    finally:
        srv.server.stop()


def test_tool_output_is_only_shortened_never_rewritten(tmp_path):
    srv = _server(tmp_path)
    try:
        status, answer = _post(srv, "edit", {"ops": [{"op": "edit", "id": 3, "text": "frei erfunden"}]})
        assert status == 409 and ("kurz" in answer["grund"].lower() or "kürz" in answer["grund"].lower())
        assert srv.host.context_writes == []
        status, out = _post(srv, "edit", {"ops": [{"op": "shorten", "id": 3, "max_chars": 4}]})
        assert status == 200, out
        assert srv.host.conversation[2]["content"].startswith("ausg")
        assert len(srv.host.conversation[2]["content"]) < 40
    finally:
        srv.server.stop()


def test_shortening_drops_the_old_wire_bytes(tmp_path):
    conversation = _transcript()
    conversation[2]["api_content"] = "ausgabe A in voller Länge, so wie sie zum Anbieter ging"
    srv = _server(tmp_path, conversation=conversation)
    try:
        status, out = _post(srv, "edit", {"ops": [{"op": "shorten", "id": 3, "max_chars": 4}]})
        assert status == 200, out
        assert "api_content" not in srv.host.conversation[2]
    finally:
        srv.server.stop()


# ── 2b. Bearbeiten nie aus einer gekürzten oder geschwärzten Vorschau ───────────────────────

def test_a_message_longer_than_the_list_cap_is_only_removable(tmp_path):
    long_text = "y" * 9000                               # > 8 KB: the list shows only the head
    srv = _server(tmp_path, conversation=[_row(1, "user", long_text), _row(2, "assistant", "ok")])
    try:
        item = _inspect(srv)["items"][0]
        assert item["truncated"] is True and item["editable"] == "remove" and item["edit_grund"]
        status, full = call(srv.port, "GET", session_path("context", "text", "0"), srv.key)
        assert status == 200 and full["editable"] is False and full["base"] is None
        # even with the right hash of the whole text the server refuses: nothing is written
        status, answer = _post(srv, "edit", {"ops": [{"op": "edit", "id": 1, "text": "kurz",
                                                      "base": _base(long_text)}]})
        assert status == 409 and answer["error"] == "not editable"
        assert srv.host.context_writes == [] and srv.host.conversation[0]["content"] == long_text
        status, out = _post(srv, "edit", {"ops": [{"op": "remove", "id": 1}]})
        assert status == 200, out                        # removing stays possible
    finally:
        srv.server.stop()


def test_a_message_carrying_a_secret_is_only_removable(tmp_path):
    text = "mein Schlüssel " + SECRET
    srv = _server(tmp_path, conversation=[_row(1, "user", text), _row(2, "assistant", "ok")])
    try:
        item = _inspect(srv)["items"][0]
        assert item["truncated"] is False and item["editable"] == "remove"
        assert SECRET not in item["text"]
        status, full = call(srv.port, "GET", session_path("context", "text", "0"), srv.key)
        assert full["editable"] is False and full["base"] is None and SECRET not in full["text"]
        # saving the masked view back would write the mask over the key: refused
        status, answer = _post(srv, "edit", {"ops": [{"op": "edit", "id": 1, "text": full["text"],
                                                      "base": _base(text)}]})
        assert status == 409 and answer["error"] == "not editable"
        assert srv.host.context_writes == [] and srv.host.conversation[0]["content"] == text
    finally:
        srv.server.stop()


@pytest.mark.parametrize("base", [None, "", "0" * 64, "nicht-hex"])
def test_an_edit_without_the_base_of_the_whole_text_is_refused(tmp_path, base):
    srv = _server(tmp_path)
    try:
        op = {"op": "edit", "id": 1, "text": "bessere Frage"}
        if base is not None:
            op["base"] = base
        status, answer = _post(srv, "edit", {"ops": [op]})
        assert status == 409 and answer["error"] == "stale"
        assert srv.host.context_writes == [] and srv.host.conversation[0]["content"] == "erste Frage"
    finally:
        srv.server.stop()


def test_an_orphaned_tool_result_is_refused_and_nothing_is_written(tmp_path):
    conversation = [_row(1, "user", "frage"), _row(2, "tool", "ohne Aufruf", tool_call_id="weg")]
    srv = _server(tmp_path, conversation=conversation)
    try:
        status, out = _post(srv, "edit", {"ops": [{"op": "remove", "id": 1}]})
        assert status == 409 and out["error"] == "tool pair"
        # the PRE-check names the broken pair before anything is touched (not the post-check)
        assert "weg" in out["grund"] and "reparieren" in out["grund"]
        assert srv.host.context_writes == []
        assert _inspect(srv)["editable"] is False
    finally:
        srv.server.stop()


def test_an_operation_that_would_leave_half_a_pair_is_refused_by_the_last_check(tmp_path, monkeypatch):
    """Second layer: whatever an operation did, a result without its call is never written."""
    from plugins.platforms.werkbank import kontext

    def _buggy_drop(messages, index):          # forgets the partner (a future bug in _drop)
        messages.pop(index)
        return 1

    monkeypatch.setattr(kontext, "_drop", _buggy_drop)
    srv = _server(tmp_path)
    try:
        status, out = _post(srv, "edit", {"ops": [{"op": "remove", "id": 2}]})   # the call, not its result
        assert status == 409 and out["error"] == "tool pair"
        assert "ohne ihren Aufruf" in out["grund"]
        assert srv.host.context_writes == []
        assert kontext.counts(tmp_path / "plugin-data" / "werkbank" / "kontext", SID) == 0
    finally:
        srv.server.stop()


def test_a_system_line_in_the_transcript_is_read_only(tmp_path):
    conversation = _transcript() + [_row(7, "system", "Hinweis eines Plugins")]
    srv = _server(tmp_path, conversation=conversation)
    try:
        items = _inspect(srv)["items"]
        assert items[-1]["role"] == "system" and items[-1]["editable"] == "none"
        status, out = _post(srv, "edit", {"ops": [{"op": "remove", "id": 7}]})
        assert status == 409 and out["error"] == "read only"
        assert srv.host.context_writes == []
    finally:
        srv.server.stop()


# ── 3. Schnappschuss und Rückgängig ────────────────────────────────────────────────────────

def test_snapshot_is_written_before_the_change_and_undo_restores_it_exactly(tmp_path):
    srv = _server(tmp_path)
    try:
        before = json.loads(json.dumps(srv.host.conversation))
        status, out = _post(srv, "edit", {"ops": [{"op": "remove", "id": 3},
                                                  {"op": "edit", "id": 5, "text": "anders",
                                                   "base": _base("zweite Frage")}]})
        assert status == 200, out
        snap = Path(out["snapshot"])
        assert snap.is_file()
        assert stat.S_IMODE(snap.stat().st_mode) == 0o600
        assert oct(stat.S_IMODE(snap.parent.stat().st_mode)) == "0o700"
        assert snap.parent.parent.name == "kontext"                  # <home>/plugin-data/werkbank/kontext
        kept = json.loads(snap.read_text(encoding="utf-8"))
        assert kept["session_id"] == SID and kept["grund"] == "edit"
        assert kept["messages"][0]["content"] == "erste Frage"

        status, undone = _post(srv, "undo", {})
        assert status == 200, undone
        assert undone["ok"] is True and undone["restored"] == len(before)
        assert srv.host.conversation == before                        # exactly the old state
        assert not snap.exists()                                     # the snapshot is used up
        assert _inspect(srv)["undo"] == 0
        status, again = _post(srv, "undo", {})
        assert status == 409 and again["error"] == "no snapshot"
    finally:
        srv.server.stop()


def test_undo_of_a_compression_snapshot_restores_it_too(tmp_path):
    srv = _server(tmp_path)
    try:
        before = json.loads(json.dumps(srv.host.conversation))
        status, out = _post(srv, "edit", {"ops": [{"op": "remove", "id": 1}]})
        assert status == 200, out
        status, undone = _post(srv, "undo", {})
        assert status == 200 and srv.host.conversation == before
    finally:
        srv.server.stop()


# ── 4. Schreibschutz: laufender Zug, fremder Schreiber, Rechte ─────────────────────────────

def test_while_a_turn_runs_the_editor_is_read_only(tmp_path):
    srv = _server(tmp_path)
    try:
        srv.host.running = True
        out = _inspect(srv)
        assert out["state"] == "arbeitet" and out["editable"] is False and out["grund"]
        status, answer = _post(srv, "edit", {"ops": [{"op": "remove", "id": 1}]})
        assert status == 409 and answer["error"] == "locked"
        assert "Zug" in answer["grund"] and srv.host.context_writes == []
    finally:
        srv.server.stop()


def test_another_writer_of_the_session_is_refused(tmp_path):
    srv = _server(tmp_path)
    try:
        srv.host.foreign = "Diese Sitzung schreibt gerade ein anderes Hermes-Fenster."
        out = _inspect(srv)
        assert out["editable"] is False
        status, answer = _post(srv, "edit", {"ops": [{"op": "remove", "id": 1}]})
        assert status == 409 and answer["error"] == "locked"
        assert srv.host.context_writes == []
    finally:
        srv.server.stop()


@pytest.mark.parametrize("path", ["edit", "undo", "compress"])
def test_an_unresolved_session_and_a_missing_key_are_refused(tmp_path, path):
    srv = _server(tmp_path)
    try:
        # no key at all: 401 before anything is looked at
        status, answer = call(srv.port, "POST", session_path(path), None, {})
        assert status == 401
        # a session the plugin never resolved: 404
        status, answer = call(srv.port, "POST",
                              session_path(path, key="agent:main:telegram:dm:9999:1"), srv.key, {})
        assert status == 404 and answer["error"]
        assert srv.host.context_writes == [] and srv.host.compress_calls == []
    finally:
        srv.server.stop()


# ── 5. Komprimieren mit Anweisung ─────────────────────────────────────────────────────────

def test_compression_hands_the_instruction_and_the_kept_turns_to_the_core(tmp_path):
    srv = _server(tmp_path)
    try:
        srv.host.compress_result = {"status": "compressed", "before_tokens": 84_000,
                                    "after_tokens": 21_000, "removed": 12, "kept": 4,
                                    "lines": ["Compressed: 16 → 4 messages"]}
        status, out = _post(srv, "compress", {"instruction": "nur die Entscheidungen", "keep": 3,
                                              "preview": False})
        assert status == 200, out
        assert srv.host.compress_calls == [("nur die Entscheidungen", 3, False)]
        assert out["before"] == 84_000 and out["after"] == 21_000 and out["removed"] == 12
        # the status line takes the new length from the compressor's own report
        _, snap = call(srv.port, "GET", session_path("snapshot"), srv.key)
        assert snap["session"]["context_tokens"] == 21_000
        assert snap["session"]["context_source"] == "kompression"
    finally:
        srv.server.stop()


def test_preview_asks_the_core_and_changes_nothing(tmp_path):
    srv = _server(tmp_path)
    try:
        srv.host.compress_result = {"status": "preview", "before_tokens": 84_000,
                                    "after_tokens": 30_000, "removed": 10, "kept": 6,
                                    "lines": ["Preview — no changes made."]}
        before = json.loads(json.dumps(srv.host.conversation))
        status, out = _post(srv, "compress", {"instruction": "Thema", "keep": 0, "preview": True})
        assert status == 200, out
        assert out["preview"] is True and out["before"] > out["after"]
        assert srv.host.compress_calls == [("Thema", None, True)]        # 0 = alles komprimieren
        assert srv.host.conversation == before and srv.host.context_writes == []
    finally:
        srv.server.stop()


def test_without_a_compress_hook_in_the_core_the_plugin_says_so(tmp_path):
    srv = _server(tmp_path)
    try:
        status, out = _post(srv, "compress", {"instruction": "", "keep": 2, "preview": True})

        assert status == 501 and "Kompress" in out["grund"]
    finally:
        srv.server.stop()


def test_instruction_is_folded_to_one_line_and_capped(tmp_path):
    srv = _server(tmp_path)
    try:
        srv.host.compress_result = {"status": "preview", "before_tokens": 10, "after_tokens": 5,
                                    "removed": 1, "kept": 1, "lines": []}
        long = "Thema\nmit\tZeilen\x00und Steuerzeichen " + "z" * 5_000
        status, out = _post(srv, "compress", {"instruction": long, "keep": 2, "preview": True})

        assert status == 200, out
        focus, keep, preview = srv.host.compress_calls[0]
        assert "\n" not in focus and "\x00" not in focus and len(focus) <= 1000
    finally:
        srv.server.stop()


# ── 6. Die Produktionsseite (host.py) gegen einen gefälschten Runner ───────────────────────

class _FakeDB:
    def __init__(self):
        self.rows = _transcript()
        self.calls = []

    def get_messages_as_conversation(self, session_id, **kw):
        self.calls.append(("read", session_id, kw))
        return [dict(r) for r in self.rows]

    def get_session(self, session_id):
        return {"session_id": session_id, "system_prompt": "Du bist Hermes."}

    def replace_messages(self, session_id, messages, **kw):
        self.calls.append(("write", session_id, kw, messages))
        self.rows = [dict(m) for m in messages]


class _FakeRunner:
    def __init__(self, *, hook=True):
        self._running = True
        self.session_store = SimpleNamespace(lookup_by_session_key=lambda k: None,
                                             lookup_by_session_id=lambda i: None,
                                             peek_session_id=lambda k: None)
        self._db = _FakeDB()
        self._session_db = self._db
        self._running_agents = {}
        self._evicted = []
        self._compress_kwargs = None
        if hook:
            self._compress_from_plugin = self._hook
        self._gateway_loop = None

    def _hook(self, **kwargs):
        self._compress_kwargs = kwargs
        return {"status": "preview", "before_tokens": 1, "after_tokens": 1, "removed": 0, "kept": 0,
                "lines": []}

    def _evict_cached_agent(self, session_key):
        self._evicted.append(session_key)

    def _cached_agent_for(self, session_key, lockless_fallback=False):
        return None


def _host(runner):
    from plugins.platforms.werkbank.host import GatewayHost
    host = GatewayHost(SimpleNamespace(_manager=SimpleNamespace(
        _gateway_message_injector=(runner, lambda *a, **k: True))), Path("/tmp")
    )
    return host


def test_host_reads_the_conversation_the_next_turn_would_load():
    runner = _FakeRunner()
    host = _host(runner)
    rows = host.context_messages(KEY_SESSION, SID)
    assert [r["role"] for r in rows] == ["user", "assistant", "tool", "assistant", "user", "assistant"]
    assert runner._db.calls[0][0] == "read" and runner._db.calls[0][1] == SID
    assert runner._db.calls[0][2]["include_row_ids"] is True
    assert runner._db.calls[0][2]["repair_alternation"] is True


def test_host_writes_rewind_style_and_evicts_the_cached_agent():
    runner = _FakeRunner()
    host = _host(runner)
    ok, grund = host.context_write(KEY_SESSION, SID, _transcript()[:2])
    assert ok is True and grund == ""
    write = [c for c in runner._db.calls if c[0] == "write"][0]
    assert write[2] == {"archive_dropped": True, "reject_active_turn_lease": True,
                        "active_only": False} or (write[2].get("archive_dropped") is True
                                                  and write[2].get("reject_active_turn_lease") is True)
    assert runner._evicted == [KEY_SESSION]


def test_host_gate_refuses_while_a_turn_runs_and_names_a_foreign_writer():
    runner = _FakeRunner()
    host = _host(runner)
    # no running turn, no foreign writer
    assert host.write_gate(KEY_SESSION) == (True, "")
    runner._running_agents = {KEY_SESSION: object()}
    ok, grund = host.write_gate(KEY_SESSION)
    assert ok is False and "Zug" in grund
    runner._running_agents = {}
    runner._foreign_writer = lambda key: "Ein anderes Hermes-Fenster schreibt diese Sitzung."
    ok, grund = host.write_gate(KEY_SESSION)
    assert ok is False and "Fenster" in grund


def test_host_compression_without_the_core_hook_returns_none():
    runner = _FakeRunner(hook=False)
    host = _host(runner)
    assert host.compress(KEY_SESSION, "Thema", 2, True) is None


def test_host_reads_the_system_prompt_of_the_session_row():
    runner = _FakeRunner()
    host = _host(runner)
    prompt, agent = host.context_system(KEY_SESSION, SID)
    assert prompt == "Du bist Hermes." and agent is None


# ── 7. Gegen eine ECHTE SessionDB (tmp_path, nie die echte state.db) ──────────────────────────

def _real_db(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(SID, "telegram")
    db.append_message(SID, "user", "erste Frage")
    db.append_message(SID, "assistant", "", tool_calls=[_call("call_a")])
    db.append_message(SID, "tool", "ausgabe A", tool_call_id="call_a", tool_name="shell")
    db.append_message(SID, "assistant", "Antwort eins")
    db.append_message(SID, "user", "zweite Frage")
    db.append_message(SID, "assistant", "Antwort zwei")
    return db


def _view(db):
    """What the next turn loads (the gateway's own read), without the bookkeeping keys."""
    keep = ("role", "content", "tool_calls", "tool_call_id")
    return [{k: m.get(k) for k in keep if m.get(k) not in (None, "")}
            for m in db.get_messages_as_conversation(SID)]


def _real_env(tmp_path):
    from plugins.platforms.werkbank import buffer

    db = _real_db(tmp_path)
    runner = _FakeRunner()
    runner._db = runner._session_db = db
    host = _host(runner)
    buffers = buffer.Buffers(plugin_id="platforms/werkbank", watermark=lambda *a, **k: None)
    buffers.watch(KEY_SESSION, SID, "telegram")
    return db, runner, host, buffers, tmp_path / "kontext"


def test_real_db_removing_a_tool_result_changes_what_the_next_turn_loads(tmp_path):
    from plugins.platforms.werkbank import kontext

    db, runner, host, buffers, snaps = _real_env(tmp_path)
    before = _view(db)
    tool_row = [m for m in host.context_messages(KEY_SESSION, SID) if m.get("role") == "tool"][0]
    out = kontext.edit(host, buffers, snaps, KEY_SESSION, [{"op": "remove", "id": tool_row["_row_id"]}])
    after = _view(db)
    assert out["removed"] == 2                              # the result AND its call went
    assert [m["role"] for m in after] == ["user", "assistant", "user", "assistant"]
    assert not any(m.get("tool_calls") or m.get("tool_call_id") for m in after)
    assert runner._evicted == [KEY_SESSION]                 # the next turn rebuilds from disk
    # the old rows stay on disk (archived, rewind-style), nothing is destroyed
    assert len(db.get_messages_as_conversation(SID, include_inactive=True)) >= len(before)

    restored = kontext.undo(host, buffers, snaps, KEY_SESSION)
    assert restored["ok"] is True
    assert _view(db) == before                              # exactly the old state again


def test_real_db_editing_a_message_is_what_the_next_turn_sees(tmp_path):
    from plugins.platforms.werkbank import kontext

    db, _runner, host, buffers, snaps = _real_env(tmp_path)
    first = host.context_messages(KEY_SESSION, SID)[0]
    kontext.edit(host, buffers, snaps, KEY_SESSION, [{"op": "edit", "id": first["_row_id"],
                                                      "text": "erste Frage, ohne Geheimnis",
                                                      "base": _base("erste Frage")}])
    assert _view(db)[0] == {"role": "user", "content": "erste Frage, ohne Geheimnis"}
    assert len(_view(db)) == 6


def test_real_db_a_foreign_lease_is_the_fence_the_gate_cannot_see(tmp_path):
    """The core has no ``_foreign_writer`` hook: the gate answers „frei“ while another process
    holds the session's turn lease — the DB lease in ``replace_messages`` is what refuses."""
    db, runner, host, _buffers, _snaps = _real_env(tmp_path)
    before = _view(db)
    assert db.try_acquire_session_turn_lease(SID, "anderes-fenster:7", ttl_seconds=60)
    assert host.write_gate(KEY_SESSION) == (True, "")           # honest: the gate does not see it
    ok, grund = host.context_write(KEY_SESSION, SID, _transcript()[:2])
    assert ok is False and "Zug" in grund
    assert _view(db) == before and runner._evicted == []


def test_real_db_a_held_turn_lease_refuses_the_write_and_keeps_the_history(tmp_path):
    from plugins.platforms.werkbank import kontext

    db, _runner, host, buffers, snaps = _real_env(tmp_path)
    before = _view(db)
    assert db.try_acquire_session_turn_lease(SID, "anderer-zug:1", ttl_seconds=60)
    first = host.context_messages(KEY_SESSION, SID)[0]
    with pytest.raises(kontext.KontextFehler) as err:
        kontext.edit(host, buffers, snaps, KEY_SESSION, [{"op": "remove", "id": first["_row_id"]}])
    assert err.value.status == 409 and "Zug" in err.value.grund
    assert _view(db) == before
    assert kontext.counts(snaps, SID) == 0                  # the refused change leaves no snapshot


def test_real_db_undo_after_the_history_moved_on_is_refused(tmp_path):
    """A turn after the edit: „Rückgängig“ would silently drop it — refused, nothing written."""
    from plugins.platforms.werkbank import kontext

    db, _runner, host, buffers, snaps = _real_env(tmp_path)
    first = host.context_messages(KEY_SESSION, SID)[0]
    kontext.edit(host, buffers, snaps, KEY_SESSION, [{"op": "edit", "id": first["_row_id"], "text": "neu",
                                                      "base": _base("erste Frage")}])
    db.append_message(SID, "user", "dritte Frage")
    db.append_message(SID, "assistant", "Antwort drei")
    moved = _view(db)
    with pytest.raises(kontext.KontextFehler) as err:
        kontext.undo(host, buffers, snaps, KEY_SESSION)
    assert err.value.status == 409 and err.value.error == "moved on"
    assert _view(db) == moved
    assert kontext.counts(snaps, SID) == 1                  # the snapshot stays for the owner to see


def test_real_db_write_with_a_moved_fingerprint_is_stale_and_writes_nothing(tmp_path):
    """``replace_messages`` has no compare-and-swap: the host re-reads right before writing."""
    from plugins.platforms.werkbank import kontext

    db, _runner, host, _buffers, _snaps = _real_env(tmp_path)
    before = _view(db)
    current = host.context_messages(KEY_SESSION, SID)
    ok, grund = host.context_write(KEY_SESSION, SID, current[:1], expected="0" * 64)
    assert ok is False and grund == kontext.WRITE_STALE
    assert _view(db) == before


# ── Nachtrag: kein Lost-Update, Zusammenfassung nicht editierbar ────────────────────────────
def test_two_concurrent_edits_both_land_or_one_is_refused_never_lost(tmp_path):
    import threading
    import time

    srv = _server(tmp_path)
    try:
        read = srv.host.context_messages

        def slow_read(session_key, session_id):
            out = read(session_key, session_id)
            time.sleep(0.05)                                # widen the read → write window
            return out

        srv.host.context_messages = slow_read
        barrier = threading.Barrier(2)
        results: Dict[int, Any] = {}

        def run(rid: int, old: str, new: str):
            barrier.wait()
            results[rid] = _post(srv, "edit", {"ops": [{"op": "edit", "id": rid, "text": new,
                                                        "base": _base(old)}]})

        threads = [threading.Thread(target=run, args=(1, "erste Frage", "Frage EINS")),
                   threading.Thread(target=run, args=(5, "zweite Frage", "Frage ZWEI"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        statuses = {rid: status for rid, (status, _a) in results.items()}
        assert set(statuses) == {1, 5} and set(statuses.values()) <= {200, 409}, results
        assert 200 in statuses.values()
        texts = [m["content"] for m in srv.host.conversation]
        for rid, wanted, index in ((1, "Frage EINS", 0), (5, "Frage ZWEI", 4)):
            if statuses[rid] == 200:
                assert texts[index] == wanted, (results, texts)   # an acknowledged edit survives
        assert len(srv.host.context_writes) == list(statuses.values()).count(200)
    finally:
        srv.server.stop()


def test_a_transcript_moved_between_read_and_write_is_409_stale(tmp_path):
    srv = _server(tmp_path)
    try:
        read = srv.host.context_messages
        calls = []

        def moving_read(session_key, session_id):
            out = read(session_key, session_id)
            if not calls:                                   # another writer lands after the read
                srv.host.conversation = srv.host.conversation + [_row(7, "user", "fremd")]
            calls.append(1)
            return out

        srv.host.context_messages = moving_read
        status, answer = _post(srv, "edit", {"ops": [{"op": "edit", "id": 1, "text": "neu",
                                                      "base": _base("erste Frage")}]})
        assert status == 409 and answer["error"] == "stale", answer
        assert srv.host.context_writes == []
        assert srv.host.conversation[0]["content"] == "erste Frage"
        assert srv.host.conversation[-1]["content"] == "fremd"
        assert _inspect(srv)["undo"] == 0                   # the refused change leaves no snapshot
    finally:
        srv.server.stop()


def test_a_held_session_lock_answers_busy_instead_of_racing(tmp_path, monkeypatch):
    from plugins.platforms.werkbank import kontext

    srv = _server(tmp_path)
    monkeypatch.setattr(kontext, "LOCK_WAIT_S", 0.05)
    lock = kontext._session_lock(KEY_SESSION)
    assert lock.acquire(timeout=1)
    try:
        for path, body in (("edit", {"ops": [{"op": "remove", "id": 1}]}), ("undo", {}),
                           ("compress", {"instruction": "x"})):
            status, answer = _post(srv, path, body)
            assert status == 409 and answer["error"] == "busy", (path, answer)
        assert srv.host.context_writes == [] and srv.host.compress_calls == []
    finally:
        lock.release()
        srv.server.stop()


def test_a_compression_summary_is_not_editable_only_removable(tmp_path):
    summary = "[Zusammenfassung] bisher besprochen: A und B"
    srv = _server(tmp_path, conversation=[_row(1, "user", summary, _compressed_summary=True),
                                          _row(2, "assistant", "ok"),
                                          _row(3, "user", "weiter"), _row(4, "assistant", "gut")])
    try:
        status, answer = _post(srv, "edit", {"ops": [{"op": "edit", "id": 1, "text": "erfunden",
                                                      "base": _base(summary)}]})
        assert status == 409 and answer["error"] == "not editable", answer
        assert srv.host.context_writes == [] and srv.host.conversation[0]["content"] == summary
        status, out = _post(srv, "edit", {"ops": [{"op": "remove", "id": 1}]})
        assert status == 200, out
    finally:
        srv.server.stop()

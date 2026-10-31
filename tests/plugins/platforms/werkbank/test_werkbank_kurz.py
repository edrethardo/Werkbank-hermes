"""WB-1388: ``GET /v1/s/{key}/kurz`` — the chat list's light read.

* same auth as the snapshot (no key / wrong key → 401, a foreign key → 404);
* only the newest rows are read (the host is asked for a handful, never the 200 of the snapshot);
* state, pending counts, last user/assistant time, the last message cut to 200 characters AFTER the
  redaction (a secret in it never leaves);
* it is fast even for a very large session (nothing but the newest rows is serialised).
"""

from __future__ import annotations

import time

from werkbank_testkit import KEY_SESSION, SID, FakeHost, call, running_server, session_path

SECRET = "sk-ant-" + "api03-" + "A" * 30          # a provider key the display must never show


def _server(tmp_path, host):
    from plugins.platforms.werkbank import snapshot

    srv = running_server(tmp_path, host)
    srv.buffers.watch(KEY_SESSION, SID, "telegram")
    assert snapshot  # imported: the module under test exists
    return srv


def test_kurz_needs_the_key_and_a_known_session(tmp_path):
    srv = _server(tmp_path, FakeHost())
    try:
        assert call(srv.port, "GET", session_path("kurz"))[0] == 401
        assert call(srv.port, "GET", session_path("kurz"), "falsch")[0] == 401
        assert call(srv.port, "GET", session_path("kurz", key="agent:main:telegram:dm:1:1"), srv.key)[0] == 404
    finally:
        srv.server.stop()


def test_kurz_names_state_counts_times_and_the_last_message(tmp_path):
    host = FakeHost()
    host.messages = [
        {"id": 1, "role": "user", "content": "Frage", "timestamp": 100.0, "platform_message_id": "7"},
        {"id": 2, "role": "assistant", "content": "Antwort eins", "timestamp": 110.0},
        {"id": 3, "role": "user", "content": "Noch eine Frage", "timestamp": 120.0},
        {"id": 4, "role": "tool", "content": "x" * 5000, "timestamp": 121.0, "tool_call_id": "t", "tool_name": "x"},
        {"id": 5, "role": "assistant", "content": "A" * 500, "timestamp": 130.0}]
    host.approvals = [{"request_id": "r1", "command": "ls"}]
    host.running = True
    srv = _server(tmp_path, host)
    try:
        status, body = call(srv.port, "GET", session_path("kurz"), srv.key)
        assert status == 200
        assert body["state"] == "arbeitet" and body["running"] is True
        assert body["pending_approvals"] == 1 and body["pending_clarify"] == 0
        assert body["last_user_ts"] == 120.0 and body["last_assistant_ts"] == 130.0
        assert body["last"] == {"role": "assistant", "text": "A" * 200, "ts": 130.0, "attachments": False}
        assert "messages" not in body and body["session_id"] == SID
        host.running, host.approvals = False, []
        body = call(srv.port, "GET", session_path("kurz"), srv.key)[1]
        assert body["state"] == "wartet" and body["running"] is False and body["pending_approvals"] == 0
    finally:
        srv.server.stop()


def test_kurz_redacts_before_it_cuts(tmp_path):
    host = FakeHost()
    host.messages = [{"id": 1, "role": "user", "content": "key " + SECRET + " " + "x" * 10_000,
                      "timestamp": 1.0}]
    srv = _server(tmp_path, host)
    try:
        body = call(srv.port, "GET", session_path("kurz"), srv.key)[1]
        assert SECRET not in body["last"]["text"] and "AAAAAAAAAA" not in body["last"]["text"]
        assert len(body["last"]["text"]) <= 200
        # the secret sits at the very edge of the cut: still not a half key
        host.messages = [{"id": 1, "role": "assistant", "content": "y" * 190 + SECRET, "timestamp": 2.0}]
        text = call(srv.port, "GET", session_path("kurz"), srv.key)[1]["last"]["text"]
        assert "AAAAAAAA" not in text
    finally:
        srv.server.stop()


def test_kurz_reads_only_a_few_rows_and_is_fast_for_a_huge_session(tmp_path):
    host = FakeHost()
    host.messages = [{"id": i, "role": "user" if i % 2 else "assistant", "content": "text " * 8000,
                      "timestamp": float(i)} for i in range(1, 3001)]    # ~120 KB per row, 3000 rows
    asked = []
    original = host.history
    host.history = lambda key, sid, limit: (asked.append(limit), original(key, sid, limit))[1]
    srv = _server(tmp_path, host)
    try:
        call(srv.port, "GET", session_path("kurz"), srv.key)            # warm up
        t0 = time.perf_counter()
        status, body = call(srv.port, "GET", session_path("kurz"), srv.key)
        took = time.perf_counter() - t0
        assert status == 200 and body["last"]["ts"] == 3000.0
        assert max(asked) <= 20, asked
        assert took < 0.25, took          # the contract is < 50 ms; the margin is for a loaded CI box
        print(f"kurz: {took * 1000:.1f} ms")
    finally:
        srv.server.stop()


def test_kurz_reads_wider_when_the_newest_rows_are_all_tool_rows(tmp_path):
    host = FakeHost()
    host.messages = [{"id": 1, "role": "user", "content": "baue das", "timestamp": 1.0},
                     {"id": 2, "role": "assistant", "content": "mache ich", "timestamp": 2.0}]
    host.messages += [{"id": i, "role": "tool", "content": "ausgabe", "tool_call_id": f"c{i}",
                       "timestamp": float(i)} for i in range(3, 40)]
    srv = _server(tmp_path, host)
    try:
        status, body = call(srv.port, "GET", session_path("kurz"), srv.key)
        assert status == 200 and body["last"] is not None, body
        assert body["last"]["ts"] == 2.0 and body["last_assistant_ts"] == 2.0
    finally:
        srv.server.stop()


def test_a_buffered_message_without_a_row_counts(tmp_path):
    host = FakeHost()
    host.messages = [{"id": 1, "role": "assistant", "content": "alt", "timestamp": 5.0}]
    srv = _server(tmp_path, host)
    try:
        from werkbank_testkit import core_event
        srv.buffers.on_event(core_event(1, "user.message", {"text": "neu vom Brett", "client_message_id": "wb-1"},
                                        id_="wb-1", origin="platforms/werkbank"))
        body = call(srv.port, "GET", session_path("kurz"), srv.key)[1]
        assert body["last"]["role"] == "user" and body["last"]["text"] == "neu vom Brett"
        assert body["last_user_ts"] is not None and body["last_assistant_ts"] == 5.0
        host.messages = [{"id": 2, "role": "user", "content": "[Werkbank] neu vom Brett", "timestamp": 9.0,
                          "display_metadata": {"client_message_id": "wb-1"}}]     # its row arrives: one message
        body = call(srv.port, "GET", session_path("kurz"), srv.key)[1]
        assert body["last"]["text"] == "[Werkbank] neu vom Brett" and body["last_user_ts"] == 9.0
    finally:
        srv.server.stop()

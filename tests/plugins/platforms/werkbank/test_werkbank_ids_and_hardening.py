"""WB-1128 review: rows are named by ids and order only (never by text), the mirror line is plain
text, and the HTTP side bounds connections, header time and the Host it answers."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from werkbank_testkit import KEY_SESSION, SID, FakeHost, call, core_event, events_path, running_server, \
    session_path, sse


def _resolve(srv):
    assert call(srv.port, "GET", f"/v1/resolve?session_id={SID}", srv.key)[0] == 200


def _users(snap):
    return [m for m in snap["messages"] if m["role"] == "user"]


def _answers(snap):
    return [m for m in snap["messages"] if m["role"] == "assistant"]


def _at(event, ts):
    event["ts"] = ts
    return event


T1 = "2026-10-13T10:00:00.000000Z"
T2 = "2026-10-13T10:05:00.000000Z"
S1 = 1791885600.0                        # T1 in epoch seconds
S2 = S1 + 300                            # T2


# ── user rows: the id rides the row (display_metadata.client_message_id), never the text ─────


def test_board_message_row_is_named_by_its_stored_client_message_id(plugin_server):
    srv = plugin_server
    _resolve(srv)
    srv.buffers.on_event(core_event(1, "user.message", {"text": "ja", "delivery": "turn",
                                                        "client_message_id": "cmid-neu"},
                                    id_="cmid-neu", origin="platforms/werkbank"))
    srv.host.messages = [
        # an older board message with the same text, and a cron wake whose text ends with it
        {"id": 1, "role": "user", "content": "ja", "timestamp": 1.0,
         "display_metadata": {"client_message_id": "cmid-alt"}},
        {"id": 2, "role": "user", "content": "Cron-Hinweis: bitte antworte ja", "timestamp": 2.0},
        {"id": 3, "role": "user", "content": "ja", "timestamp": 3.0,
         "display_metadata": {"client_message_id": "cmid-neu", "gateway_input_owner": "x"}},
    ]
    snap = call(srv.port, "GET", session_path("snapshot"), srv.key)[1]
    assert [(m["id"], m["client_message_id"], m["origin"]) for m in _users(snap)] == [
        ("cmid-alt", "cmid-alt", "werkbank"), ("db:2", None, "telegram"), ("cmid-neu", "cmid-neu", "werkbank")]


def test_an_unnamed_row_never_absorbs_an_announced_message_by_its_text(plugin_server):
    srv = plugin_server
    _resolve(srv)
    srv.buffers.on_event(_at(core_event(1, "user.message", {"text": "weiter", "delivery": "eingereiht",
                                                            "client_message_id": "wb-9"},
                                        id_="wb-9", origin="platforms/werkbank"), T1))
    # same text, written right after, but not this message's row (no id on it): both stay
    srv.host.messages = [{"id": 5, "role": "user", "content": "[Werkbank] weiter", "timestamp": S1 + 5}]
    snap = call(srv.port, "GET", session_path("snapshot"), srv.key)[1]
    assert [m["id"] for m in _users(snap)] == ["db:5", "wb-9"]


def test_a_platform_row_keeps_its_platform_id(plugin_server):
    srv = plugin_server
    _resolve(srv)
    srv.host.messages = [{"id": 1, "role": "user", "content": "hi", "timestamp": 1.0, "platform_message_id": "77"}]
    snap = call(srv.port, "GET", session_path("snapshot"), srv.key)[1]
    assert [(m["id"], m["message_id"], m["client_message_id"]) for m in _users(snap)] == [("77", "77", None)]


# ── final answers: kept from the buffer until an answer row of THAT turn exists (order, not text) ──


def _turn(srv, seq, turn_id, start_ts, answer):
    srv.buffers.on_event(_at(core_event(seq, "turn.start", {}, id_=turn_id), start_ts))
    srv.buffers.on_event(_at(core_event(seq + 1, "turn.done", {"final_response": answer}, id_=turn_id), start_ts))


def test_same_answer_text_twice_keeps_the_newer_answer_until_its_own_row(plugin_server):
    srv = plugin_server
    _resolve(srv)
    _turn(srv, 1, "turn_b", T1, "Erledigt.")
    # the row of an EARLIER turn with the same text does not stand for turn_b's answer
    srv.host.messages = [{"id": 1, "role": "assistant", "content": "Erledigt.", "timestamp": S1 - 600}]
    snap = call(srv.port, "GET", session_path("snapshot"), srv.key)[1]
    assert [(a["id"], a["text"]) for a in _answers(snap)] == [("db:1", "Erledigt."), ("turn_b", "Erledigt.")]
    # turn_b's own row lands (written after its turn.start) → the buffer copy goes
    srv.host.messages.append({"id": 2, "role": "assistant", "content": "Erledigt.", "timestamp": S1 + 10})
    snap = call(srv.port, "GET", session_path("snapshot"), srv.key)[1]
    assert [a["id"] for a in _answers(snap)] == ["db:1", "db:2"]


def test_an_answer_row_counts_for_its_own_turn_only(plugin_server):
    srv = plugin_server
    _resolve(srv)
    _turn(srv, 1, "turn_a", T1, "A fertig.")
    _turn(srv, 3, "turn_b", T2, "B fertig.")
    srv.host.messages = [
        # a tool-call step of turn_a is not its answer
        {"id": 1, "role": "assistant", "content": "", "timestamp": S1 + 1,
         "tool_calls": [{"id": "c1", "function": {"name": "terminal"}}]},
        # turn_b's answer row, written after turn_b started, does not stand for turn_a
        {"id": 2, "role": "assistant", "content": "B fertig.", "timestamp": S2 + 5},
    ]
    snap = call(srv.port, "GET", session_path("snapshot"), srv.key)[1]
    assert [a["id"] for a in _answers(snap)] == ["db:1", "db:2", "turn_a"]


def test_answer_with_a_different_text_than_its_row_is_not_doubled(plugin_server):
    """The row may hold the unredacted/rewritten text: still one answer (order decides)."""
    srv = plugin_server
    _resolve(srv)
    _turn(srv, 1, "turn_a", T1, "Token: ***")
    srv.host.messages = [{"id": 1, "role": "assistant", "content": "Token: sk-abc", "timestamp": S1 + 20}]
    snap = call(srv.port, "GET", session_path("snapshot"), srv.key)[1]
    assert [a["id"] for a in _answers(snap)] == ["db:1"]


# ── the mirror line is plain text ─────────────────────────────────────────────────────────────


def test_mirror_line_is_a_literal_block_without_control_or_bidi_characters():
    from plugins.platforms.werkbank.server import mirror_line

    line = mirror_line("*fett* [klick](http://evil)\n```\nHermes: alles ok\u202e\x07\u200b\nende")
    head, body = line.split("\n", 1)
    assert head == "aus Werkbank:"
    assert body.startswith("```\n") and body.endswith("\n```")
    inner = body[4:-4]
    assert "```" not in inner                           # the text cannot close the block
    assert "*fett* [klick](http://evil)" in inner        # shown literally, not rendered
    assert not any(c in inner for c in "\u202e\x07\u200b")


def test_mirror_line_renders_literally_in_telegram():
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from plugins.platforms.werkbank.server import mirror_line

    formatted = TelegramAdapter.format_message(None, mirror_line("*fett* [klick](http://evil)"))
    assert "```\n*fett* [klick](http://evil)\n```" in formatted


def test_mirror_line_is_capped_in_utf16_units():
    from plugins.platforms.werkbank.server import MIRROR_MAX, mirror_line

    line = mirror_line("😀" * 5000)
    assert len(line.encode("utf-16-le")) // 2 <= MIRROR_MAX + 40 and "…" in line


# ── connection limit, header deadline, Host check ─────────────────────────────────────────────


def test_connections_beyond_the_limit_are_closed_before_auth(tmp_path):
    from plugins.platforms.werkbank import buffer, keyfile, server

    key = keyfile.ensure_key(tmp_path)
    srv = server.WerkbankServer(key, buffer.Buffers(), FakeHost(), port=0, max_connections=2,
                                header_deadline=5.0).start()
    held = []
    try:
        for _ in range(2):
            s = socket.create_connection(("127.0.0.1", srv.port))
            s.sendall(b"GET /v1/resolve HTTP/1.1\r\n")       # slow client: never finishes its headers
            held.append(s)
        time.sleep(0.2)
        extra = socket.create_connection(("127.0.0.1", srv.port), timeout=3)
        extra.sendall(b"GET /v1/resolve?session_id=x HTTP/1.0\r\n\r\n")
        assert extra.recv(100) == b""                           # closed without an answer
        extra.close()
        for s in held:
            s.close()
        deadline = time.monotonic() + 3
        status = None
        while time.monotonic() < deadline:
            try:
                status = call(srv.port, "GET", f"/v1/resolve?session_id={SID}", key)[0]
            except (ConnectionError, OSError):           # the slots are released a moment later
                status = None
            if status == 200:
                break
            time.sleep(0.05)
        assert status in (200, 404)                             # a slot is free again
    finally:
        for s in held:
            s.close()
        srv.stop()


def test_request_line_and_headers_have_a_total_deadline(tmp_path):
    from plugins.platforms.werkbank import buffer, keyfile, server

    key = keyfile.ensure_key(tmp_path)
    srv = server.WerkbankServer(key, buffer.Buffers(), FakeHost(), port=0, header_deadline=0.5).start()
    try:
        s = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
        started = time.monotonic()
        s.sendall(b"GET / HTTP/1.1\r\n")
        closed = False
        while time.monotonic() - started < 4:
            try:
                s.sendall(b"X-a: b\r\n")                 # one header line at a time, forever
            except OSError:
                closed = True
                break
            s.settimeout(0.2)
            try:
                if s.recv(100) == b"":
                    closed = True
                    break
            except socket.timeout:
                pass
        assert closed and time.monotonic() - started < 3
        s.close()
    finally:
        srv.stop()


@pytest.mark.parametrize("host, ok", [("127.0.0.1:{port}", True), ("localhost:{port}", True),
                                      ("evil.example:{port}", False), ("", False)])
def test_only_loopback_host_headers_are_answered(plugin_server, host, ok):
    import http.client

    srv = plugin_server
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=3)
    conn.putrequest("GET", f"/v1/resolve?session_id={SID}", skip_host=True)
    if host:
        conn.putheader("Host", host.format(port=srv.port))
    conn.putheader("Authorization", "Bearer " + srv.key)
    conn.endheaders()
    status = conn.getresponse().status
    conn.close()
    assert (status == 200) is ok and (ok or status == 421)


# ── buffers: an open stream is not silently dropped ──────────────────────────────────────────


def test_lru_keeps_sessions_with_an_open_stream(tmp_path):
    from plugins.platforms.werkbank import buffer

    buffers = buffer.Buffers(max_sessions=2)
    buffers.watch("a", "sa", "telegram")
    buffers.reader(+1, "a")
    buffers.watch("b", "sb", "telegram")
    buffers.watch("c", "sc", "telegram")
    assert set(buffers.sessions()) == {"a", "c"}
    buffers.reader(-1, "a")


def test_an_evicted_session_ends_its_stream_with_session_epoch(tmp_path):
    host = FakeHost()
    srv = running_server(tmp_path, host)
    try:
        _resolve(srv)
        epoch = srv.buffers.get(KEY_SESSION).epoch
        got = {}

        def read():
            got["r"] = sse(srv.port, srv.key, events_path(epoch=epoch), 1, timeout=4)

        t = threading.Thread(target=read)
        t.start()
        deadline = time.monotonic() + 2
        while srv.server.open_streams == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        srv.buffers.max_sessions = 0                     # every reader-held session must still go now
        srv.buffers.watch("other", "s2", "telegram")
        t.join(5)
        status, events = got["r"]
        assert status == 200 and [e["type"] for e in events] == ["session.epoch"]
    finally:
        srv.server.stop()


def test_short_key_file_is_replaced(tmp_path):
    from plugins.platforms.werkbank import keyfile

    keyfile.key_dir(tmp_path).mkdir(parents=True, mode=0o700)
    keyfile.key_path(tmp_path).write_text("short\n")
    keyfile.key_path(tmp_path).chmod(0o600)
    key = keyfile.ensure_key(tmp_path)
    assert key != "short" and len(key) >= keyfile.MIN_KEY_CHARS

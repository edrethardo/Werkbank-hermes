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


def _at(event, ts):
    event["ts"] = ts
    return event


T1 = "2026-10-13T10:00:00.000000Z"
S1 = 1791885600.0                        # T1 in epoch seconds


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
         "display_metadata": {"client_message_id": "cmid-alt", "hermes_plugin_id": "platforms/werkbank"}},
        {"id": 2, "role": "user", "content": "Cron-Hinweis: bitte antworte ja", "timestamp": 2.0},
        {"id": 3, "role": "user", "content": "ja", "timestamp": 3.0,
         "display_metadata": {"client_message_id": "cmid-neu", "hermes_plugin_id": "platforms/werkbank",
                              "gateway_input_owner": "x"}},
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


# Which row stands for a buffered answer (row ids, message_uid; never the clock or the text):
# test_werkbank_row_order.py.


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


def test_when_full_the_oldest_unauthenticated_connection_goes_not_the_new_one(tmp_path):
    from plugins.platforms.werkbank import buffer, keyfile, server

    key = keyfile.ensure_key(tmp_path)
    srv = server.WerkbankServer(key, buffer.Buffers(), FakeHost(), port=0, max_connections=2,
                                header_deadline=5.0).start()
    held = []
    try:
        for _ in range(2):
            s = socket.create_connection(("127.0.0.1", srv.port), timeout=3)
            s.sendall(b"GET /v1/resolve HTTP/1.1\r\n")       # slow client: never finishes its headers
            held.append(s)
            time.sleep(0.05)
        time.sleep(0.2)
        # a client with complete headers gets in at once; the OLDEST idle connection is closed
        assert call(srv.port, "GET", f"/v1/resolve?session_id={SID}", key, timeout=2)[0] == 200
        assert held[0].recv(100) == b""
    finally:
        for s in held:
            s.close()
        srv.stop()


def test_a_reconnect_flood_cannot_lock_out_a_client_with_complete_headers(tmp_path):
    """Reviewer repro (wb1128_dos.py): every slot held and re-opened as soon as it is closed."""
    from plugins.platforms.werkbank import buffer, keyfile, server

    key = keyfile.ensure_key(tmp_path)
    srv = server.WerkbankServer(key, buffer.Buffers(), FakeHost(), port=0, max_connections=8,
                                header_deadline=10.0).start()

    def hold():
        s = socket.create_connection(("127.0.0.1", srv.port))
        s.sendall(b"GET / HTTP/1.1\r\n")
        return s

    socks = [hold() for _ in range(8)]
    try:
        time.sleep(0.2)
        ok = 0
        for _ in range(10):
            for i, s in enumerate(socks):                    # the attacker refills every lost slot
                s.setblocking(False)
                try:
                    if s.recv(1) == b"":
                        s.close()
                        socks[i] = hold()
                except BlockingIOError:
                    pass
                except OSError:
                    s.close()
                    socks[i] = hold()
            try:
                ok += call(srv.port, "GET", "/v1/resolve?session_id=x", key, timeout=1)[0] == 404
            except OSError:
                pass
        assert ok == 10
    finally:
        for s in socks:
            s.close()
        srv.stop()


def test_authenticated_connections_have_their_own_quota(tmp_path):
    from plugins.platforms.werkbank import buffer, keyfile, server

    key = keyfile.ensure_key(tmp_path)
    host = FakeHost()
    buffers = buffer.Buffers()
    srv = server.WerkbankServer(key, buffers, host, port=0, max_connections=2, max_authenticated=1,
                                header_deadline=5.0, heartbeat=0.1).start()
    try:
        assert call(srv.port, "GET", f"/v1/resolve?session_id={SID}", key)[0] == 200
        got = {}
        path = events_path(epoch=buffers.get(KEY_SESSION).epoch)          # a live stream, held open
        t = threading.Thread(target=lambda: got.setdefault("r", sse(srv.port, key, path, 50, timeout=1.5)))
        t.start()
        deadline = time.monotonic() + 2
        while srv.open_streams == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        # the stream holds the one authenticated place: a second authorized request is refused
        # politely; unauthenticated newcomers never evict the authenticated stream
        assert call(srv.port, "GET", f"/v1/resolve?session_id={SID}", key)[0] == 503
        for _ in range(4):
            s = socket.create_connection(("127.0.0.1", srv.port))
            s.sendall(b"GET / HTTP/1.1\r\n")
            s.close()
        t.join(5)
        assert got["r"][0] == 200
    finally:
        srv.stop()


def test_a_client_that_hangs_up_early_leaves_no_traceback(tmp_path, capfd):
    from plugins.platforms.werkbank import buffer, keyfile, server

    key = keyfile.ensure_key(tmp_path)
    srv = server.WerkbankServer(key, buffer.Buffers(), FakeHost(), port=0, max_connections=1,
                                header_deadline=5.0).start()
    try:
        for _ in range(20):
            s = socket.create_connection(("127.0.0.1", srv.port))
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
            s.sendall(b"GET /v1/resolve?session_id=x HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            s.close()                                           # RST: the answer hits a dead socket
        time.sleep(0.5)
        assert call(srv.port, "GET", "/v1/resolve?session_id=x", key)[0] == 404
    finally:
        srv.stop()
    err = capfd.readouterr().err
    assert "Traceback" not in err


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

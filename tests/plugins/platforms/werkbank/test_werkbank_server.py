"""WB-1128: contract of the plugin server against a fake gateway host (``FakeHost``)."""

from __future__ import annotations

import logging
import os
import stat

import pytest

from werkbank_testkit import (KEY_SESSION, SID, FakeHost, call, core_event, events_path, running_server,
                              session_path, sse)


def _resolve(srv):
    status, answer = call(srv.port, "GET", f"/v1/resolve?session_id={SID}", srv.key)
    assert status == 200, answer
    return srv.buffers.get(KEY_SESSION).epoch


# ── epoch / after outside the buffer ────────────────────────────────────────────────────────


def test_after_outside_the_ring_gives_only_session_epoch_then_snapshot_fits(tmp_path):
    srv = running_server(tmp_path, FakeHost(), ring=10)
    try:
        epoch = _resolve(srv)
        for n in range(1, 26):
            srv.buffers.on_event(core_event(n))
        status, events = sse(srv.port, srv.key, events_path(epoch=epoch, after=2), count=5, timeout=1.0)
        assert status == 200
        assert [e["type"] for e in events] == ["session.epoch"]          # the ONLY event, stream ends
        assert events[0]["session_key"] == KEY_SESSION and events[0]["payload"]["grund"] == "after"

        status, snap = call(srv.port, "GET", session_path("snapshot"), srv.key)
        assert status == 200
        assert (snap["epoch"], snap["seq"]) == (epoch, 25)
        srv.buffers.on_event(core_event(26, "turn.done", {"final_response": "x"}, id_="turn_1"))
        status, events = sse(srv.port, srv.key, events_path(epoch=snap["epoch"], after=snap["seq"]),
                             count=1)
        assert [(e["type"], e["seq"], e["epoch"]) for e in events] == [("turn.done", 26, epoch)]
    finally:
        srv.server.stop()


def test_wrong_epoch_gives_only_session_epoch(plugin_server):
    srv = plugin_server
    epoch = _resolve(srv)
    srv.buffers.on_event(core_event(1))
    status, events = sse(srv.port, srv.key, events_path(epoch=epoch - 1, after=0), count=5, timeout=1.0)
    assert [e["type"] for e in events] == ["session.epoch"]
    assert events[0]["epoch"] == epoch and events[0]["payload"]["grund"] == "epoch"
    status, snap = call(srv.port, "GET", session_path("snapshot"), srv.key)
    assert (snap["epoch"], snap["seq"]) == (epoch, 1)


def test_after_beyond_the_end_is_outside_the_buffer(plugin_server):
    srv = plugin_server
    epoch = _resolve(srv)
    status, events = sse(srv.port, srv.key, events_path(epoch=epoch, after=7), count=5, timeout=1.0)
    assert [e["type"] for e in events] == ["session.epoch"]


@pytest.mark.parametrize("trigger,reason", [
    (lambda b: b.on_event(core_event(5)), "luecke"),
    (lambda b: b.on_event(core_event(None, "overflow", {"dropped": 3, "first_seq": 2, "last_seq": 4})),
     "overflow"),
    (lambda b: b.on_event(core_event(2, sid="20261013_130000_newone")), "session_id"),
])
def test_new_epoch_on_gap_overflow_and_session_change(plugin_server, trigger, reason):
    srv = plugin_server
    epoch = _resolve(srv)
    srv.buffers.on_event(core_event(1))
    trigger(srv.buffers)
    buf = srv.buffers.get(KEY_SESSION)
    assert buf.epoch > epoch and buf.epoch_reason == reason
    status, events = sse(srv.port, srv.key, events_path(epoch=epoch, after=1), count=3, timeout=1.0)
    assert [e["type"] for e in events] == ["session.epoch"]
    assert events[0]["payload"]["ursache"] == reason


def test_overflow_then_the_core_continues_without_a_second_epoch(plugin_server):
    srv = plugin_server
    _resolve(srv)
    srv.buffers.on_event(core_event(1))
    srv.buffers.on_event(core_event(None, "overflow", {"dropped": 2, "first_seq": 2, "last_seq": 3}))
    epoch = srv.buffers.get(KEY_SESSION).epoch
    srv.buffers.on_event(core_event(4))
    buf = srv.buffers.get(KEY_SESSION)
    assert buf.epoch == epoch and buf.seq == 1


def test_an_open_stream_ends_with_session_epoch_when_the_epoch_changes(plugin_server):
    import threading
    import time

    srv = plugin_server
    epoch = _resolve(srv)
    got = {}
    thread = threading.Thread(target=lambda: got.update(r=sse(srv.port, srv.key, events_path(epoch=epoch),
                                                              count=5, timeout=3.0)))
    thread.start()
    time.sleep(0.3)
    srv.buffers.on_event(core_event(1))
    time.sleep(0.3)
    srv.buffers.on_event(core_event(9))            # gap in core_seq
    thread.join(5)
    status, events = got["r"]
    assert [e["type"] for e in events] == ["text.delta", "session.epoch"]
    assert events[1]["payload"]["grund"] == "neu" and events[1]["epoch"] > epoch


def test_plugin_restart_starts_a_new_epoch(tmp_path):
    from plugins.platforms.werkbank import buffer

    first = buffer.Buffers(epoch_start=buffer.initial_epoch(1_800_000_000))
    first.watch(KEY_SESSION, SID, "telegram")
    for _ in range(30):                            # epochs raised during the first process
        first.new_epoch(KEY_SESSION, "overflow")
    second = buffer.Buffers(epoch_start=buffer.initial_epoch(1_800_000_001))
    assert second.watch(KEY_SESSION, SID, "telegram").epoch > first.get(KEY_SESSION).epoch
    assert len(str(buffer.initial_epoch())) <= 12          # the board's checked_int limit


# ── allowlist ───────────────────────────────────────────────────────────────────────────────


def test_only_resolved_sessions_are_buffered(plugin_server):
    srv = plugin_server
    srv.buffers.on_event(core_event(1, key="agent:main:telegram:dm:999"))
    srv.buffers.on_event(core_event(1))
    assert srv.buffers.sessions() == []
    status, answer = call(srv.port, "GET", session_path("snapshot"), srv.key)
    assert status == 404
    status, _ = call(srv.port, "GET", events_path(), srv.key)
    assert status == 404
    _resolve(srv)
    assert srv.buffers.sessions() == [KEY_SESSION]


def test_resolve_unknown_session_is_404_and_a_moved_id_still_resolves(plugin_server):
    srv = plugin_server
    assert call(srv.port, "GET", "/v1/resolve?session_id=nope_123", srv.key)[0] == 404
    assert call(srv.port, "GET", "/v1/resolve?session_id=bad%20id", srv.key)[0] == 400
    _resolve(srv)
    new_sid = "20261013_140000_moved"
    srv.host.sessions = {new_sid: {"session_key": KEY_SESSION, "session_id": new_sid,
                                   "platform": "telegram", "live": True}}
    status, answer = call(srv.port, "GET", f"/v1/resolve?session_id={SID}", srv.key)
    assert status == 200 and answer["session_key"] == KEY_SESSION and answer["session_id"] == new_sid
    assert srv.buffers.get(KEY_SESSION).epoch_reason == "session_id"


# ── key, bind address ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("header", [None, "Bearer wrong", "Bearer ", "bearer {key}", "{key}"])
def test_missing_or_wrong_key_is_401_everywhere(plugin_server, header):
    srv = plugin_server
    key = None if header is None else header.format(key=srv.key)
    for method, path in (("GET", f"/v1/resolve?session_id={SID}"), ("GET", session_path("snapshot")),
                         ("GET", events_path()), ("POST", session_path("messages")),
                         ("POST", session_path("stop"))):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=3)
        headers = {} if key is None else {"Authorization": key}
        conn.request(method, path, body=b"{}" if method == "POST" else None, headers=headers)
        resp = conn.getresponse()
        assert resp.status == 401, (method, path)
        assert srv.key not in resp.read().decode()
        conn.close()


def test_server_binds_loopback_only(plugin_server):
    assert plugin_server.server.address[0] == "127.0.0.1"
    from plugins.platforms.werkbank import server
    assert server.HOST == "127.0.0.1" and server.DEFAULT_PORT == 8767


def test_key_file_is_0600_in_a_0700_directory_and_reused(tmp_path):
    from plugins.platforms.werkbank import keyfile

    key = keyfile.ensure_key(tmp_path)
    path = tmp_path / "plugin-data" / "werkbank" / "key"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    assert len(key) >= 40 and keyfile.ensure_key(tmp_path) == key
    os.chmod(path, 0o644)
    os.chmod(path.parent, 0o755)
    assert keyfile.ensure_key(tmp_path) == key
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_key_file_symlink_is_refused(tmp_path):
    from plugins.platforms.werkbank import keyfile

    target = tmp_path / "elsewhere"
    target.write_text("x")
    keyfile.key_dir(tmp_path).mkdir(parents=True)
    (keyfile.key_dir(tmp_path) / "key").symlink_to(target)
    with pytest.raises(OSError):
        keyfile.ensure_key(tmp_path)


# ── send, stop, 501 ─────────────────────────────────────────────────────────────────────────


def test_send_accepts_once_and_refuses_a_repeated_id(plugin_server):
    srv = plugin_server
    _resolve(srv)
    body = {"text": "bitte prüfen", "client_message_id": "wb-1"}
    assert call(srv.port, "POST", session_path("messages"), srv.key, body) == (200, {"accepted": True})
    status, answer = call(srv.port, "POST", session_path("messages"), srv.key, body)
    assert status == 409 and answer["accepted"] is False and answer["grund"]
    assert srv.host.injected == [(KEY_SESSION, "bitte prüfen", "wb-1")]       # never re-sent


def test_send_refused_by_the_gateway_is_503_with_a_reason_and_may_be_resent(plugin_server):
    srv = plugin_server
    _resolve(srv)
    srv.host.inject_answer = False
    body = {"text": "hallo", "client_message_id": "wb-2"}
    status, answer = call(srv.port, "POST", session_path("messages"), srv.key, body)
    assert status == 503 and answer["accepted"] is False and "allow_gateway_injection" in answer["grund"]
    # WB-1148: the mirror goes out first (its id anchors the answer); a posted one is taken back,
    # see test_refused_message_takes_back_its_mirror
    assert [m[1] for m in srv.host.mirrored] == ["hallo"]
    srv.host.inject_answer = True
    assert call(srv.port, "POST", session_path("messages"), srv.key, body)[0] == 200


@pytest.mark.parametrize("body", [{}, {"text": "", "client_message_id": "a"}, {"text": "x"},
                                  {"text": "x", "client_message_id": "bad id"},
                                  {"text": "x" * 200_001, "client_message_id": "a"}])
def test_send_validates_text_and_id(plugin_server, body):
    srv = plugin_server
    _resolve(srv)
    assert call(srv.port, "POST", session_path("messages"), srv.key, body)[0] in (400, 413)
    assert srv.host.injected == []


@pytest.mark.parametrize("mirror", [True, False])
def test_mirror_line_follows_the_switch(tmp_path, mirror):
    srv = running_server(tmp_path, FakeHost(), mirror=mirror)
    try:
        _resolve(srv)
        call(srv.port, "POST", session_path("messages"), srv.key, {"text": "Deploy?", "client_message_id": "m1"})
        # the raw text goes to the host (it knows the chat's platform and owner); the format is mirror.py's
        assert srv.host.mirrored == ([(KEY_SESSION, "Deploy?", "")] if mirror else [])
    finally:
        srv.server.stop()


def test_mirror_message_id_becomes_the_reply_anchor_of_the_injected_turn(tmp_path):
    host = FakeHost()
    host.mirror = lambda key, text, name="": host.mirrored.append((key, text, name)) or "900"
    srv = running_server(tmp_path, host)
    try:
        _resolve(srv)
        call(srv.port, "POST", session_path("messages"), srv.key, {"text": "Deploy?", "client_message_id": "m1"})
        assert host.anchors == ["900"] and host.mirrored == [(KEY_SESSION, "Deploy?", "")]
    finally:
        srv.server.stop()


def test_mirror_that_raises_still_injects_without_anchor(tmp_path):
    host = FakeHost()

    def boom(*_a, **_kw):
        raise RuntimeError("down")

    host.mirror = boom
    srv = running_server(tmp_path, host)
    try:
        _resolve(srv)
        assert call(srv.port, "POST", session_path("messages"), srv.key,
                    {"text": "Deploy?", "client_message_id": "m1"}) == (200, {"accepted": True})
        assert host.anchors == [None]
    finally:
        srv.server.stop()


def test_stop_uses_interrupt_session_or_answers_501(plugin_server):
    srv = plugin_server
    _resolve(srv)
    assert call(srv.port, "POST", session_path("stop"), srv.key, {}) == (200, {"stopped": True})
    srv.host.interrupt_answer = False
    assert call(srv.port, "POST", session_path("stop"), srv.key, {}) == (200, {"stopped": False})
    srv.host.interrupt_answer = None
    status, answer = call(srv.port, "POST", session_path("stop"), srv.key, {})
    assert status == 501 and "interrupt_session" in answer["grund"]


def test_approvals_and_clarify_of_nothing_pending_are_404(plugin_server):
    """WB-1129 answers them (test_werkbank_approvals.py); an id nothing waits on is 404."""
    srv = plugin_server
    _resolve(srv)
    for path, body in ((session_path("approvals", "req1"), {"choice": "once"}),
                       (session_path("clarify", "c1"), {"answer": "ja"})):
        status, answer = call(srv.port, "POST", path, srv.key, body)
        assert status == 404 and isinstance(answer["grund"], str)


def test_tool_output_from_the_history(plugin_server):
    srv = plugin_server
    _resolve(srv)
    srv.host.outputs["call_9"] = "volle Ausgabe"
    assert call(srv.port, "GET", session_path("tools", "call_9", "output"), srv.key) == (
        200, {"tool_call_id": "call_9", "output": "volle Ausgabe"})
    assert call(srv.port, "GET", session_path("tools", "call_x", "output"), srv.key)[0] == 404


# ── no chat text in the log ─────────────────────────────────────────────────────────────────


def test_no_chat_text_in_the_log(plugin_server, caplog, capfd):
    srv = plugin_server
    secret = "GEHEIMTEXT-4711-chat"
    caplog.set_level(logging.DEBUG)
    epoch = _resolve(srv)
    srv.host.messages = [{"id": 1, "role": "user", "content": secret, "timestamp": 1.0}]
    srv.buffers.on_event(core_event(1, "user.message", {"text": secret, "delivery": "turn"}))
    srv.buffers.on_event(core_event(2, "text.delta", {"text": secret}))
    call(srv.port, "POST", session_path("messages"), srv.key, {"text": secret, "client_message_id": "s1"})
    srv.host.inject_answer = False
    call(srv.port, "POST", session_path("messages"), srv.key, {"text": secret, "client_message_id": "s2"})
    call(srv.port, "GET", session_path("snapshot"), srv.key)
    sse(srv.port, srv.key, events_path(epoch=epoch), count=2)
    call(srv.port, "GET", f"/v1/resolve?session_id={SID}", "wrong")
    out, err = capfd.readouterr()
    assert secret not in caplog.text and secret not in out and secret not in err
    assert srv.key not in caplog.text and srv.key not in err


# ── the snapshot is complete up to its seq (WB-1132 review) ─────────────────────────────────


def _users(snap):
    return [m for m in snap["messages"] if m["role"] == "user"]


def test_snapshot_holds_an_announced_message_before_its_db_row_exists(plugin_server):
    srv = plugin_server
    _resolve(srv)
    srv.buffers.on_event(core_event(1, "user.message", {"text": "später bitte", "delivery": "eingereiht",
                                                        "client_message_id": "wb-9"},
                                    id_="wb-9", origin="platforms/werkbank"))
    status, snap = call(srv.port, "GET", session_path("snapshot"), srv.key)
    assert snap["seq"] == 1
    assert [(m["id"], m["client_message_id"], m["origin"], m["delivery"]) for m in _users(snap)] == [
        ("wb-9", "wb-9", "werkbank", "eingereiht")]
    # the row arrives carrying the id the gateway stored on it → still ONE message, the row
    srv.host.messages = [{"id": 5, "role": "user", "content": "[Werkbank] später bitte", "timestamp": 2e9,
                          "display_metadata": {"client_message_id": "wb-9"}}]
    status, snap = call(srv.port, "GET", session_path("snapshot"), srv.key)
    assert [(m["id"], m["db_id"]) for m in _users(snap)] == [("wb-9", 5)]


def test_same_text_twice_is_not_hidden_by_an_older_row(plugin_server):
    srv = plugin_server
    _resolve(srv)
    srv.host.messages = [{"id": 1, "role": "user", "content": "ok", "timestamp": 1.0}]
    srv.buffers.on_event(core_event(1, "user.message", {"text": "ok", "delivery": "turn", "message_id": "tg-2"},
                                    id_="tg-2"))
    snap = call(srv.port, "GET", session_path("snapshot"), srv.key)[1]
    assert [m["id"] for m in _users(snap)] == ["db:1", "tg-2"]


def test_snapshot_after_turn_done_has_the_final_answer_before_the_db_row(plugin_server):
    srv = plugin_server
    _resolve(srv)
    srv.buffers.on_event(core_event(1, "turn.start", {}, id_="turn_a"))
    srv.buffers.on_event(core_event(2, "turn.done", {"final_response": "Fertig, alle Tests grün."}, id_="turn_a"))
    snap = call(srv.port, "GET", session_path("snapshot"), srv.key)[1]
    answers = [m for m in snap["messages"] if m["role"] == "assistant"]
    assert [(a["id"], a["text"], a["from_buffer"]) for a in answers] == [
        ("turn_a", "Fertig, alle Tests grün.", True)]
    assert snap["state"] == "wartet" and snap["seq"] == 2
    srv.host.messages = [{"id": 7, "role": "assistant", "content": "Fertig, alle Tests grün.", "timestamp": 2e9}]
    snap = call(srv.port, "GET", session_path("snapshot"), srv.key)[1]
    assert [a["id"] for a in snap["messages"] if a["role"] == "assistant"] == ["db:7"]


def test_refused_message_takes_back_its_mirror(tmp_path):
    host = FakeHost()
    host.inject_answer = False
    host.mirror = lambda key, text, name="": "900"
    srv = running_server(tmp_path, host)
    try:
        _resolve(srv)
        assert call(srv.port, "POST", session_path("messages"), srv.key,
                    {"text": "Deploy?", "client_message_id": "m1"})[0] == 503
        assert host.unmirrored == [(KEY_SESSION, "900")]
    finally:
        srv.server.stop()

"""WB-1128 red test first: a client that aborts a stream and reconnects with ``after=N`` gets
exactly the events it missed — none twice, none lost."""

from __future__ import annotations

from werkbank_testkit import KEY_SESSION, SID, call, core_event, events_path, sse


def _watch(srv):
    status, answer = call(srv.port, "GET", f"/v1/resolve?session_id={SID}", srv.key)
    assert status == 200, answer
    assert answer["session_key"] == KEY_SESSION
    return srv.buffers.get(KEY_SESSION).epoch


def test_reconnect_after_abort_gets_exactly_the_missing_events(plugin_server):
    srv = plugin_server
    epoch = _watch(srv)
    for n in range(1, 6):
        srv.buffers.on_event(core_event(n, payload={"text": f"t{n}"}))

    status, first = sse(srv.port, srv.key, events_path(epoch=epoch, after=0), count=3)
    assert status == 200
    assert [e["seq"] for e in first] == [1, 2, 3]          # the client hangs up after three

    srv.buffers.on_event(core_event(6, "tool.start", {"tool_name": "terminal"}, id_="call_1"))
    srv.buffers.on_event(core_event(7, "tool.end", {"tool_name": "terminal", "output": "ok"}, id_="call_1"))

    status, rest = sse(srv.port, srv.key, events_path(epoch=epoch, after=first[-1]["seq"]), count=10,
                       timeout=1.0)
    assert status == 200
    assert [e["seq"] for e in rest] == [4, 5, 6, 7]         # exactly the missing ones, no doubles
    assert [e["type"] for e in rest] == ["text.delta", "text.delta", "tool.start", "tool.end"]
    for event in first + rest:
        assert event["session_key"] == KEY_SESSION
        assert event["epoch"] == epoch and event["v"] == 1
    assert rest[2]["id"] == "call_1"


def test_live_events_reach_an_open_stream_in_order(plugin_server):
    import threading
    import time

    srv = plugin_server
    epoch = _watch(srv)
    got = {}

    def reader():
        got["status"], got["events"] = sse(srv.port, srv.key, events_path(epoch=epoch, after=0), count=4)

    thread = threading.Thread(target=reader)
    thread.start()
    time.sleep(0.3)
    for n in range(1, 5):
        srv.buffers.on_event(core_event(n))
    thread.join(5)
    assert got["status"] == 200
    assert [e["seq"] for e in got["events"]] == [1, 2, 3, 4]

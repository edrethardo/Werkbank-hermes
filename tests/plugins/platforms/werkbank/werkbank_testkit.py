"""Shared helpers for the ``werkbank`` platform plugin tests (WB-1128).

Every test runs its own plugin server on ``127.0.0.1`` with port 0 and a key file under
``tmp_path``; nothing is written below ``~/.hermes``.
"""

from __future__ import annotations

import http.client
import json
import socket
import time
from types import SimpleNamespace
from typing import Any, Optional

KEY_SESSION = "agent:main:telegram:dm:4711:51"
SID = "20261013_120000_abcdef"


def core_event(seq, type_="text.delta", payload=None, *, key=KEY_SESSION, sid=SID, id_=None,
               origin="telegram", parent_id=None):
    """An event exactly as ``gateway.session_events.emit`` hands it to a subscriber."""
    return {"v": 1, "session_key": key, "session_id": sid, "platform": "telegram", "origin": origin,
            "core_seq": seq, "type": type_, "id": id_, "parent_id": parent_id,
            "ts": "2026-10-13T12:00:00.000000Z", "payload": dict(payload or {})}


class FakeHost:
    """The gateway side as the server sees it (``host.GatewayHost`` in production)."""

    def __init__(self):
        self.sessions = {SID: {"session_key": KEY_SESSION, "session_id": SID, "platform": "telegram",
                               "live": True}}
        self.injected: list = []
        self.inject_answer = True
        self.supports_client_message_id = True
        self.interrupts: list = []
        self.interrupt_answer: Optional[bool] = True   # None = interrupt_session missing (pre WB-1127)
        self.mirrored: list = []
        self.anchors: list = []            # reply anchor handed to each inject (WB-1148)
        self.unmirrored: list = []
        self.messages: list = []
        self.approvals: list = []
        self.clarify: list = []
        self.outputs: dict = {}
        self.running = False
        self.lineages: dict = {}           # session_id -> compression lineage root → tip

    def unmirror(self, session_key, message_id):
        self.unmirrored.append((session_key, message_id))

    def resolve(self, session_id):
        return self.sessions.get(session_id)

    def current_session_id(self, session_key):
        for info in self.sessions.values():
            if info["session_key"] == session_key:
                return info["session_id"]
        return None

    def inject(self, session_key, text, client_message_id, reply_to_message_id=None):
        self.injected.append((session_key, text, client_message_id))
        self.anchors.append(reply_to_message_id)
        return self.inject_answer, self.supports_client_message_id

    def interrupt(self, session_key):
        self.interrupts.append(session_key)
        return self.interrupt_answer

    def mirror(self, session_key, text, name=""):
        self.mirrored.append((session_key, text, name))
        return None                        # no message id: the answer gets no reply anchor

    def is_running(self, session_key):
        return self.running

    def session_state(self, session_key):
        return {}

    def history(self, session_key, session_id, limit):
        return list(self.messages)[-limit:]

    def row_watermark(self, session_key, session_id):
        """Highest row id written so far (the SessionDB's ids only grow: AUTOINCREMENT)."""
        return max((m["id"] for m in self.messages if isinstance(m.get("id"), int)), default=0)

    def lineage(self, session_key, session_id):
        return list(self.lineages.get(session_id) or [session_id])

    def pending_approvals(self, session_key):
        return list(self.approvals)

    def pending_clarify(self, session_key):
        return list(self.clarify)

    def tool_output(self, session_key, session_id, tool_call_id):
        return self.outputs.get(tool_call_id)


def running_server(tmp_path, fake_host, *, ring=5000, mirror=True):
    """(server, key, buffers, host) — a running plugin server on a free port."""
    from plugins.platforms.werkbank import buffer, keyfile, server

    key = keyfile.ensure_key(tmp_path)
    buffers = buffer.Buffers(plugin_id="platforms/werkbank", ring=ring, watermark=fake_host.row_watermark)
    srv = server.WerkbankServer(key, buffers, fake_host, port=0, heartbeat=0.1,
                                mirror_enabled=lambda: mirror).start()
    return SimpleNamespace(server=srv, key=key, buffers=buffers, host=fake_host, port=srv.port)


def call(port, method, path, key=None, body=None, timeout=5.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {}
    if key is not None:
        headers["Authorization"] = "Bearer " + key
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        try:
            answer: Any = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            answer = raw
        return resp.status, answer
    finally:
        conn.close()


def sse(port, key, path, count, timeout=3.0):
    """Read at most *count* events, then hang up (a client that aborts mid-stream)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request("GET", path, headers={"Authorization": "Bearer " + key})
    resp = conn.getresponse()
    events, data = [], []
    deadline = time.monotonic() + timeout       # heartbeats keep the socket busy: a total deadline
    try:
        if resp.status != 200:
            return resp.status, json.loads(resp.read() or b"{}")
        while len(events) < count and time.monotonic() < deadline:
            line = resp.readline()
            if not line:
                break
            line = line.rstrip(b"\r\n")
            if line.startswith(b"data:"):
                data.append(line[5:].strip())
            elif not line and data:
                events.append(json.loads(b"\n".join(data).decode("utf-8")))
                data = []
    except (socket.timeout, TimeoutError):
        pass
    finally:
        conn.close()
    return resp.status, events


def events_path(key=KEY_SESSION, epoch=0, after=0):
    from urllib.parse import quote
    return f"/v1/s/{quote(key, safe='')}/events?epoch={epoch}&after={after}"


def session_path(*rest, key=KEY_SESSION):
    from urllib.parse import quote
    return "/v1/s/" + "/".join(quote(p, safe="") for p in (key, *rest))

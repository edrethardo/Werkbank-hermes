"""WB-781: after a browser restart every /chat tab comes back to ITS session.

A restored tab brings its URL (``?resume=`` / ``?tab_session=``) but not its sessionStorage
attach token, so it reconnects under a new key. The old keep-alive PTY still hosts the session
and its TUI still holds the session lease; spawning a second TUI for the same session is what
produced "This chat is open in another Hermes window/terminal". The registry must hand the
restored tab its orphaned PTY instead — and keep refusing when another tab really shows it.
"""
import asyncio
import json
import time

import pytest

from hermes_cli import web_server
import hermes_cli.web_server_chat as _web_server_chat
from hermes_cli.pty_session import PtyIdentity, PtySessionRegistry, WS_CLOSE_PROCESS_EXITED


class FakeBridge:
    def __init__(self):
        self.written = bytearray()
        self.closed = False

    def read(self, timeout):
        time.sleep(min(timeout, 0.01))
        return b""

    async def write(self, data):
        self.written.extend(data)
        return True

    def resize(self, cols, rows):
        pass

    def close(self):
        self.closed = True

    def is_alive(self):
        # The fake child never exits; a test that reuses a bridge after close_all() must still
        # see it as the same live process.
        return True


class FakeWS:
    def __init__(self):
        self.sent = []
        self.close_code = None

    async def send_bytes(self, data):
        self.sent.append(("bytes", bytes(data)))

    async def send_text(self, text):
        self.sent.append(("text", text))

    async def close(self, code=1000, reason=""):
        self.close_code = code


def _identity(tab, sid, session_file=None):
    return PtyIdentity(tab=tab, variant=("", "", "", "", "", ""), resume_ids=frozenset({sid}),
                       session_file=session_file, spawn_resume=sid)


# --- websocket level: the user-visible flow ---------------------------------------------------


@pytest.fixture
def harness(monkeypatch, tmp_path):
    spawned = []

    def fake_spawn(argv, cwd=None, env=None):
        bridge = FakeBridge()
        spawned.append((argv[1], bridge))
        return bridge

    monkeypatch.setattr(_web_server_chat.PtyBridge, "spawn", staticmethod(fake_spawn))
    monkeypatch.setattr(_web_server_chat, "_ws_auth_reason", lambda ws: (None, "test"))
    monkeypatch.setattr(_web_server_chat, "_ws_host_origin_reason", lambda ws: None)
    monkeypatch.setattr(_web_server_chat, "_ws_client_reason", lambda ws: None)
    monkeypatch.setattr(_web_server_chat, "_build_sidecar_url", lambda channel: None)
    files = {}
    monkeypatch.setattr(_web_server_chat, "_active_session_file_for_channel",
                        lambda app, channel: files.setdefault(channel, tmp_path / f"{channel}.json"))

    async def fake_argv(**kw):
        resume = kw.get("resume")
        return (["x", resume or "fresh"], "/tmp", {"HERMES_TUI_RESUME": resume} if resume else {})

    monkeypatch.setattr(_web_server_chat, "_resolve_chat_argv_async", fake_argv)
    harness = type("H", (), {"spawned": spawned, "files": files})
    try:
        yield harness
    finally:
        _web_server_chat.PTY_REGISTRY._sessions.clear()


def _session_frames(ws, n=1):
    frames = []
    while len(frames) < n:
        msg = ws.receive()
        if msg.get("text"):
            data = json.loads(msg["text"])
            if data.get("type") == "session":
                frames.append(data["id"])
    return frames


def test_browser_restart_brings_each_tab_back_to_its_own_session(harness):
    from starlette.testclient import TestClient

    client = TestClient(web_server.app)
    # Before: two tabs, two sessions (tokens live in each tab's sessionStorage).
    for token, sid in (("TAB-A", "S1"), ("TAB-B", "S2")):
        with client.websocket_connect(f"/api/pty?attach={token}&resume={sid}&channel=c-{token}"
                                      "&session_frames=1") as ws:
            assert _session_frames(ws) == [sid]
    assert [sid for sid, _ in harness.spawned] == ["S1", "S2"]

    # Browser restart: URLs restored, sessionStorage (attach token) and channel are new.
    for token, sid in (("NEW-B", "S2"), ("NEW-A", "S1")):
        with client.websocket_connect(f"/api/pty?attach={token}&resume={sid}&channel=c-{token}"
                                      "&session_frames=1") as ws:
            assert _session_frames(ws) == [sid]      # the tab shows its old session ...
            ws.send_bytes(b"hi " + sid.encode())

    # ... in its OLD PTY: no second TUI that the first one's lease would refuse.
    assert [sid for sid, _ in harness.spawned] == ["S1", "S2"]
    by_sid = dict(harness.spawned)
    assert by_sid["S1"].written.endswith(b"hi S1")
    assert by_sid["S2"].written.endswith(b"hi S2")


def test_fresh_tab_learns_its_session_and_gets_it_back(harness):
    """A tab opened as plain /chat has no ?resume=; the PTY tells it which session it hosts."""
    from starlette.testclient import TestClient

    client = TestClient(web_server.app)
    with client.websocket_connect("/api/pty?attach=TAB-C&channel=c1&session_frames=1") as ws:
        ws.send_bytes(b"x")
    # The TUI writes the session it created into its active-session file.
    harness.files["c1"].write_text(json.dumps({"session_id": "S3"}))
    with client.websocket_connect("/api/pty?attach=TAB-C&channel=c1&session_frames=1") as ws:
        assert _session_frames(ws) == ["S3"]          # -> the tab stores ?tab_session=S3

    with client.websocket_connect("/api/pty?attach=RESTORED&resume=S3&channel=c2&session_frames=1") as ws:
        assert _session_frames(ws) == ["S3"]
    assert [sid for sid, _ in harness.spawned] == ["fresh"]


def test_old_clients_get_no_session_frame(harness):
    from starlette.testclient import TestClient

    client = TestClient(web_server.app)
    with client.websocket_connect("/api/pty?attach=OLD&resume=S1") as ws:
        ws.send_bytes(b"x")
    session = next(iter(_web_server_chat.PTY_REGISTRY._sessions.values()))
    assert session._announce_ws is None


def test_second_live_tab_on_the_same_session_is_not_handed_the_pty(harness):
    """Protection stays: a tab that still shows the session keeps its PTY; the other tab gets its
    own TUI, whose session lease is then refused by the owner as before."""
    from starlette.testclient import TestClient

    client = TestClient(web_server.app)
    with client.websocket_connect("/api/pty?attach=LIVE&resume=S1&channel=c1") as live:
        live.send_bytes(b"x")
        with client.websocket_connect("/api/pty?attach=OTHER&resume=S1&channel=c2") as other:
            other.send_bytes(b"y")
        assert len(harness.spawned) == 2
        live.send_bytes(b"still mine")
    assert harness.spawned[0][1].written.endswith(b"still mine")


# --- registry level: half-open viewers and the lease leak ---------------------------------------


@pytest.mark.asyncio
async def test_half_open_viewer_does_not_pin_the_pty_as_a_foreign_owner():
    """A proxied socket whose browser is gone never sees a disconnect (loopback uvicorn sends no
    pings). Silent past the stale window, it no longer counts as a live foreign owner."""
    reg = PtySessionRegistry(ttl=1800.0, max_sessions=4, buffer_cap=1024, read_timeout=0.01,
                             viewer_stale=120.0)
    old = FakeBridge()
    s, _ = await reg.attach_or_spawn("tab-A\0S1", spawn=lambda: old, identity=_identity("tab-A", "S1"))
    ghost = FakeWS()
    await s.attach(ghost)

    s.last_seen_at = time.monotonic() - 10                     # tab alive: stays protected
    spawned = []
    s2, created = await reg.attach_or_spawn("tab-X\0S1", spawn=lambda: spawned.append(1) or FakeBridge(),
                                            identity=_identity("tab-X", "S1"))
    assert created and s2 is not s
    await reg.close_all()

    reg = PtySessionRegistry(ttl=1800.0, max_sessions=4, buffer_cap=1024, read_timeout=0.01,
                             viewer_stale=120.0)
    s, _ = await reg.attach_or_spawn("tab-A\0S1", spawn=lambda: old, identity=_identity("tab-A", "S1"))
    await s.attach(ghost)
    s.last_seen_at = time.monotonic() - 600                    # browser gone, socket half-open
    restored, created = await reg.attach_or_spawn("tab-R\0S1", spawn=FakeBridge,
                                                  identity=_identity("tab-R", "S1"))
    assert not created and restored is s and restored.bridge is old
    assert set(reg._sessions) == {"tab-R\0S1"}
    await restored.attach(FakeWS())
    assert ghost.close_code is not None                        # the ghost is told it was superseded
    await reg.close_all()


@pytest.mark.asyncio
async def test_closed_without_restore_releases_within_ttl_even_when_half_open():
    """No leak: a PTY whose viewer vanished without a disconnect is reaped (its TUI — and with it
    the session lease — ends) once the TTL has passed since the last sign of life."""
    reg = PtySessionRegistry(ttl=1800.0, max_sessions=4, buffer_cap=1024, read_timeout=0.01,
                             viewer_stale=120.0)
    bridge = FakeBridge()
    s, _ = await reg.attach_or_spawn("tab-A\0S1", spawn=lambda: bridge, identity=_identity("tab-A", "S1"))
    ghost = FakeWS()
    await s.attach(ghost)
    last = time.monotonic()
    s.last_seen_at = s.last_output_at = last

    await reg.reap_idle(now=last + 1700)                       # inside the TTL: kept
    assert not bridge.closed
    await reg.reap_idle(now=last + 1800 + 1)                   # TTL after last activity: gone
    assert bridge.closed and not reg._sessions
    assert ghost.close_code == WS_CLOSE_PROCESS_EXITED


@pytest.mark.asyncio
async def test_live_viewer_is_never_reaped():
    reg = PtySessionRegistry(ttl=1.0, max_sessions=4, buffer_cap=1024, read_timeout=0.01,
                             viewer_stale=120.0)
    bridge = FakeBridge()
    s, _ = await reg.attach_or_spawn("k", spawn=lambda: bridge)
    await s.attach(FakeWS())
    await asyncio.sleep(0)
    await reg.reap_idle(now=time.monotonic() + 60)             # silent < stale window
    assert not bridge.closed
    await reg.close_all()


@pytest.mark.asyncio
async def test_different_launch_variant_is_not_adopted():
    """Same session, other profile/model: that PTY is a different chat, not this tab's."""
    reg = PtySessionRegistry(ttl=1800.0, max_sessions=4, buffer_cap=1024, read_timeout=0.01)
    s, _ = await reg.attach_or_spawn("a", spawn=FakeBridge, identity=_identity("tab-A", "S1"))
    other = PtyIdentity(tab="tab-B", variant=("work", "", "", "", "", ""), resume_ids=frozenset({"S1"}))
    s2, created = await reg.attach_or_spawn("b", spawn=FakeBridge, identity=other)
    assert created and s2 is not s
    await reg.close_all()

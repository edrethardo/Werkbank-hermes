"""The tui_gateway delivery socket: a board hands a ticket to THIS live session.

The socket is a second door into a session whose writer is the PTY/WebSocket client.
These tests pin the three promises that made the door expensive to open:

* the permission decision — who may write here — is filesystem-enforced and off by default,
* delivery reuses ``prompt.submit(queued=True)``; there is no second composer path,
* the delivering caller never becomes a writer: the live transport is untouched and a
  running turn is queued behind, never interrupted.
"""

import json
import os
import socket
import stat
import threading
import types
from pathlib import Path

import pytest

from tui_gateway import delivery_socket as ds
from tui_gateway import server


pytestmark = pytest.mark.linux_only


# ── helpers ────────────────────────────────────────────────────────────────

def _session(**extra):
    return {
        "agent": types.SimpleNamespace(),
        "session_key": "session-key",
        "history": [],
        "history_lock": threading.RLock(),
        "history_version": 0,
        "running": False,
        "transport": None,
        "attached_images": [],
        **extra,
    }


@pytest.fixture()
def home(tmp_path):
    """A SHORT home: pytest tmp paths can exceed sun_path, and the direct in-home bind is the
    case these tests are about (the temp-dir fallback has its own test)."""
    import shutil
    import tempfile

    try:
        root = Path(tempfile.mkdtemp(prefix="hdel-", dir="/tmp"))
    except OSError:
        pytest.skip("/tmp not writable on this host")
    d = root / ".hermes"
    d.mkdir()
    try:
        yield d
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def live_sessions(monkeypatch):
    sessions = {}
    monkeypatch.setattr(server, "_sessions", sessions)
    return sessions


@pytest.fixture()
def submits(monkeypatch):
    """Record every prompt.submit the delivery path makes, without running a turn."""
    calls = []

    def _fake_submit(rid, params):
        calls.append(dict(params))
        return {"jsonrpc": "2.0", "id": rid, "result": {"status": "queued"}}

    monkeypatch.setitem(server._methods, "prompt.submit", _fake_submit)
    return calls


def _ask(sock_path, request, *, timeout=5.0):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(sock_path))
        s.sendall(json.dumps(request).encode("utf-8") + b"\n")
        chunks = []
        while b"\n" not in b"".join(chunks):
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks).partition(b"\n")[0]
    return json.loads(raw.decode("utf-8")) if raw else None


@pytest.fixture()
def running_socket(home, monkeypatch):
    """A started delivery socket, with the config gate forced open."""
    monkeypatch.setattr(ds, "delivery_socket_enabled", lambda cfg=None: True)
    sock = ds.start_delivery_socket(home)
    assert sock is not None
    try:
        yield sock
    finally:
        sock.stop()


# ── the permission decision ────────────────────────────────────────────────

def test_delivery_socket_is_off_unless_configured(home, monkeypatch):
    """Opening a prompt door is opt-in: no config key, no socket.

    A status verb that any local process may read is not the same risk as a verb that
    puts words in the agent's mouth, so this one does not ship on by default.
    """
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    assert ds.delivery_socket_enabled() is False
    assert ds.start_delivery_socket(home) is None
    assert not (home / ds.DELIVERY_SOCKET_FILENAME).exists()


def test_config_gate_reads_the_tui_block(monkeypatch):
    monkeypatch.setattr(server, "_load_cfg", lambda: {"tui": {"delivery_socket": True}})
    assert ds.delivery_socket_enabled() is True
    monkeypatch.setattr(server, "_load_cfg", lambda: {"tui": {"delivery_socket": False}})
    assert ds.delivery_socket_enabled() is False


def test_socket_lives_under_hermes_home_and_is_owner_only(home, running_socket):
    """File permissions ARE the auth boundary — the same contract as gateway.sock.

    There is no token: anything that can connect may deliver, so the socket must never be
    group- or world-reachable, and it must sit inside the profile's own HERMES_HOME.
    """
    path = home / ds.DELIVERY_SOCKET_FILENAME
    assert running_socket.socket_path() == path
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"delivery socket is reachable beyond its owner: {oct(mode)}"


def test_a_deep_home_falls_back_to_a_short_path_with_a_pointer(tmp_path, monkeypatch):
    """A HERMES_HOME longer than sun_path still gets a socket — bound in the temp dir, found
    through the pointer file, and still owner-only."""
    monkeypatch.setattr(ds, "delivery_socket_enabled", lambda cfg=None: True)
    deep = tmp_path / ("d" * 120) / ".hermes"
    deep.mkdir(parents=True)
    sock = ds.start_delivery_socket(deep)
    assert sock is not None
    try:
        assert sock.socket_path() != deep / ds.DELIVERY_SOCKET_FILENAME
        pointer = deep / f"{ds.DELIVERY_SOCKET_FILENAME}.path"
        assert pointer.read_text().strip() == str(sock.socket_path())
        assert stat.S_IMODE(os.stat(sock.socket_path()).st_mode) == 0o600
        assert _ask(sock.socket_path(), {"verb": "identify", "id": 1})["ok"] is True
    finally:
        sock.stop()
    assert not pointer.exists()


def test_client_helper_reaches_the_socket(home, running_socket, live_sessions, submits):
    """The board's side of the wire: deliver_ticket resolves the socket and lands one submit."""
    live_sessions["s1"] = _session()
    response = ds.deliver_ticket(home, "WB-9: ship it", session_id="s1")
    assert response["ok"] is True and response["result"]["delivered"] is True
    assert submits == [{"session_id": "s1", "text": "WB-9: ship it", "queued": True}]


def test_client_helper_reports_a_missing_socket_instead_of_raising(tmp_path):
    assert ds.deliver_ticket(tmp_path, "hi")["ok"] is False


def test_stop_removes_the_socket_file(home, running_socket):
    path = home / ds.DELIVERY_SOCKET_FILENAME
    assert path.exists()
    running_socket.stop()
    assert not path.exists()


# ── wire contract ──────────────────────────────────────────────────────────

def test_one_request_per_connection_and_unknown_verbs_are_named(running_socket):
    response = _ask(running_socket.socket_path(), {"verb": "identify", "id": 7})
    assert response["ok"] is True and response["id"] == 7
    assert response["result"]["protocol"] == ds.DELIVERY_PROTOCOL_VERSION
    assert response["result"]["pid"] == os.getpid()

    bad = _ask(running_socket.socket_path(), {"verb": "nope", "id": 8})
    assert bad["ok"] is False
    assert "ticket.deliver" in bad["supported_verbs"]


def test_oversized_request_is_refused_without_a_turn(running_socket, live_sessions, submits):
    live_sessions["s1"] = _session()
    huge = {"verb": "ticket.deliver", "params": {"session_id": "s1", "text": "x" * (128 * 1024)}}
    response = _ask(running_socket.socket_path(), huge)
    assert response["ok"] is False and "too large" in response["error"]
    assert submits == []


# ── ticket.deliver rides the existing composer ─────────────────────────────

def test_ticket_deliver_submits_queued_through_prompt_submit(running_socket, live_sessions, submits):
    """No new composer path: the ticket text goes through prompt.submit with queued=True,
    so it runs as the NEXT turn exactly like a relayed DM does."""
    live_sessions["s1"] = _session()
    response = _ask(running_socket.socket_path(),
                    {"verb": "ticket.deliver", "id": 1,
                     "params": {"session_id": "s1", "text": "WB-1: do the thing"}})
    assert response["ok"] is True
    assert response["result"]["delivered"] is True
    assert response["result"]["session_id"] == "s1"
    assert submits == [{"session_id": "s1", "text": "WB-1: do the thing", "queued": True}]


def test_delivery_never_takes_write_ownership_of_the_session(running_socket, live_sessions, monkeypatch):
    """The PTY stays the writer. The delivering connection has no transport at all, so
    prompt.submit's re-bind finds nothing to attach and the live transport is untouched."""
    pty = object()
    live_sessions["s1"] = _session(transport=pty)
    seen = {}

    def _fake_submit(rid, params):
        from tui_gateway.transport import current_transport
        seen["transport_during_submit"] = current_transport()
        return {"jsonrpc": "2.0", "id": rid, "result": {"status": "queued"}}

    monkeypatch.setitem(server._methods, "prompt.submit", _fake_submit)
    response = _ask(running_socket.socket_path(),
                    {"verb": "ticket.deliver", "id": 1, "params": {"session_id": "s1", "text": "hi"}})

    assert response["ok"] is True
    assert seen["transport_during_submit"] is None, "delivery bound a transport — it became a second writer"
    assert live_sessions["s1"]["transport"] is pty


def test_running_turn_is_queued_not_interrupted(running_socket, live_sessions, monkeypatch):
    """Real _handle_busy_submit against a running session: queued=True forces queue mode,
    so the live turn is neither steered nor interrupted."""
    interrupted = []
    agent = types.SimpleNamespace(
        steer=lambda text: interrupted.append(("steer", text)),
        interrupt=lambda *a, **k: interrupted.append(("interrupt", a)))
    session = _session(agent=agent, running=True, transport=object())
    live_sessions["s1"] = session
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")

    response = _ask(running_socket.socket_path(),
                    {"verb": "ticket.deliver", "id": 1, "params": {"session_id": "s1", "text": "A"}})
    assert response["ok"] is True
    assert response["result"]["status"] == "queued"
    assert interrupted == []
    assert session["queued_prompt"]["text"] == "A"


def test_deliveries_keep_their_order(running_socket, live_sessions, monkeypatch):
    """Three tickets arriving during one turn reach the agent in the order they were sent.

    Consecutive text-only arrivals share one queue slot and merge losslessly (the gateway's
    existing ``_enqueue_prompt`` behaviour), so the contract is the ORDER of the words, not the
    number of envelopes.
    """
    session = _session(running=True, agent=None, transport=object())
    live_sessions["s1"] = session
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")

    for text in ("A", "B", "C"):
        response = _ask(running_socket.socket_path(),
                        {"verb": "ticket.deliver", "id": 1, "params": {"session_id": "s1", "text": text}})
        assert response["ok"] is True

    queued = "\n".join([session["queued_prompt"]["text"]]
                       + [q["text"] for q in session.get("queued_prompts", [])])
    assert queued.index("A") < queued.index("B") < queued.index("C")


# ── addressing + refusals ──────────────────────────────────────────────────

def test_single_live_session_needs_no_id(running_socket, live_sessions, submits):
    live_sessions["only"] = _session()
    response = _ask(running_socket.socket_path(),
                    {"verb": "ticket.deliver", "id": 1, "params": {"text": "hi"}})
    assert response["ok"] is True
    assert submits[0]["session_id"] == "only"


def test_ambiguous_and_missing_targets_refuse_instead_of_guessing(running_socket, live_sessions, submits):
    live_sessions["a"] = _session()
    live_sessions["b"] = _session()
    ambiguous = _ask(running_socket.socket_path(),
                     {"verb": "ticket.deliver", "id": 1, "params": {"text": "hi"}})
    assert ambiguous["ok"] is False and "session_id" in ambiguous["error"]

    unknown = _ask(running_socket.socket_path(),
                   {"verb": "ticket.deliver", "id": 2, "params": {"session_id": "ghost", "text": "hi"}})
    assert unknown["ok"] is False

    empty = _ask(running_socket.socket_path(),
                 {"verb": "ticket.deliver", "id": 3, "params": {"session_id": "a", "text": "   "}})
    assert empty["ok"] is False
    assert submits == []


def test_identify_lists_the_live_sessions_it_can_deliver_to(running_socket, live_sessions):
    live_sessions["a"] = _session(session_key="key-a")
    response = _ask(running_socket.socket_path(), {"verb": "identify", "id": 1})
    assert response["result"]["sessions"] == ["a"]


def test_a_delivery_that_errors_reports_the_rpc_error(running_socket, live_sessions, monkeypatch):
    monkeypatch.setitem(
        server._methods, "prompt.submit",
        lambda rid, params: {"jsonrpc": "2.0", "id": rid,
                             "error": {"code": 4090, "message": "session limit reached"}})
    live_sessions["s1"] = _session()
    response = _ask(running_socket.socket_path(),
                    {"verb": "ticket.deliver", "id": 1, "params": {"session_id": "s1", "text": "hi"}})
    assert response["ok"] is False
    assert "session limit reached" in response["error"]

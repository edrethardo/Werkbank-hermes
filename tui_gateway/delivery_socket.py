"""Delivery socket — the door through which a local board hands a ticket to THIS live session.

The TUI gateway owns the live sessions, so the socket belongs here: not in the dashboard (it
does not hold the session) and not in the messaging gateway (a different process with a
different job). ``gateway/control_socket.py`` is the shape this follows — a local-only socket,
one JSON request per connection, bounded request size, versioned verbs — with one deliberate
difference spelled out below.

**The permission decision.** ``gateway.sock`` answers *status* verbs: anything that can open the
file learns what the gateway is doing. ``ticket.deliver`` is not that. It puts words into the
agent's mouth and spends the user's tokens, so the same "filesystem permissions are the auth
boundary" answer had to be re-taken rather than inherited:

* The socket is owner-only (``0o600``, bound under a restrictive umask) inside the profile's own
  ``HERMES_HOME``. There is no token and no peer allowlist — a process running as the user can
  already read that home, drive the PTY, and edit ``config.yaml``; a token stored beside the
  socket would only look like a second boundary.
* Unlike the gateway's status socket, this one is **off unless configured**
  (``tui.delivery_socket: true``). An escalation from "read my status" to "type for me" is the
  user's call, not a default. This is the whole reason the verb is gated at all.
* It is never a TCP port: a listening port has no owner, and "localhost only" is not a
  permission.

**Not a second writer.** Delivery reuses ``prompt.submit(queued=True)`` — the same composer a
typed message goes through — and binds *no* transport, so the PTY/WebSocket client stays the
session's writer and the delivered prompt runs as the NEXT turn without interrupting one in
flight. Arrivals are serialized by a lock so their order survives concurrent connections.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DELIVERY_PROTOCOL_VERSION = 1
DELIVERY_SOCKET_FILENAME = "tui-delivery.sock"
# One JSON line in, one out. Bounded so a local process cannot balloon the gateway's memory.
_MAX_REQUEST_BYTES = 64 * 1024
_READ_TIMEOUT_S = 5.0
_LISTEN_BACKLOG = 16


def delivery_socket_enabled(cfg: dict | None = None) -> bool:
    """Whether ``tui.delivery_socket`` opens the door. Absent key = closed (see module docstring)."""
    from utils import is_truthy_value
    from tui_gateway import server

    root = server._load_cfg() if cfg is None else cfg
    tui = root.get("tui") if isinstance(root, dict) else {}
    return is_truthy_value((tui or {}).get("delivery_socket"), default=False)


def _live_session_ids() -> list[str]:
    from tui_gateway import server

    with server._sessions_lock:
        return list(server._sessions)


def _resolve_target(session_id: str) -> tuple[str, Optional[str]]:
    """``(session_id, error)``. An empty id resolves only when exactly one session is live —
    never a guess between several, because the wrong chat is worse than a refusal."""
    live = _live_session_ids()
    if session_id:
        return (session_id, None) if session_id in live else ("", f"no live session {session_id!r}")
    if len(live) == 1:
        return live[0], None
    if not live:
        return "", "no live session to deliver to"
    return "", f"several live sessions; name one with session_id (live: {', '.join(sorted(live))})"


class DeliverySocketServer:
    """Blocking-accept Unix socket server on its own daemon thread.

    Threads rather than asyncio: the TUI gateway's main loop is a blocking ``stdin`` read, so
    there is no event loop to join — and a delivery must not wait on one.
    """

    def __init__(self, home: Path) -> None:
        from gateway.control_socket import resolve_local_socket_path

        self._home = Path(home)
        # Same sun_path fallback as gateway.sock: a deep HERMES_HOME binds a short temp path and
        # leaves a pointer file behind, so clients resolve one way for both sockets.
        self._path, self._pointer = resolve_local_socket_path(
            self._home, DELIVERY_SOCKET_FILENAME, fallback_stem="hermes-tui-deliver")
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        # Deliveries are serialized so two boards racing on separate connections cannot
        # interleave into the session queue out of order.
        self._deliver_lock = threading.Lock()

    def socket_path(self) -> Path:
        return self._path

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> bool:
        try:
            with contextlib.suppress(OSError):
                self._path.unlink(missing_ok=True)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            # Restrictive umask so the socket is never group/world-connectable, not even for the
            # instant between bind and chmod.
            old_umask = os.umask(0o177)
            try:
                sock.bind(str(self._path))
            finally:
                os.umask(old_umask)
            os.chmod(self._path, 0o600)
            sock.listen(_LISTEN_BACKLOG)
            sock.settimeout(0.5)  # so stop() is observed promptly
            if self._pointer is not None:
                self._pointer.write_text(str(self._path), encoding="utf-8")
        except Exception as exc:
            logger.warning("TUI delivery socket failed to start (non-fatal): %s", exc)
            self.cleanup_files()
            return False
        self._sock = sock
        self._thread = threading.Thread(target=self._serve, name="tui-delivery-socket", daemon=True)
        self._thread.start()
        logger.info("TUI delivery socket listening at %s", self._path)
        return True

    def stop(self) -> None:
        self._stopping.set()
        if (sock := self._sock) is not None:
            with contextlib.suppress(Exception):
                sock.close()
        self._sock = None
        if (thread := self._thread) is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None
        self.cleanup_files()

    def cleanup_files(self) -> None:
        """Best-effort socket + pointer removal (atexit-safe)."""
        for path in filter(None, (self._path, self._pointer)):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)

    # ── serving ────────────────────────────────────────────────────────────

    def _serve(self) -> None:
        while not self._stopping.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle_connection, args=(conn,),
                             name="tui-delivery-conn", daemon=True).start()

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(_READ_TIMEOUT_S)
            raw = self._read_request_line(conn)
            if raw is None:
                self._write(conn, {"ok": False, "error": "request too large",
                                   "protocol": DELIVERY_PROTOCOL_VERSION})
                return
            if not raw:
                return
            self._write(conn, self.handle_request_line(raw))
        except (socket.timeout, ConnectionError, OSError):
            pass
        except Exception:
            logger.debug("delivery socket connection handler error", exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    @staticmethod
    def _read_request_line(conn: socket.socket) -> Optional[bytes]:
        """One newline-terminated line, or None when the peer exceeds the size cap."""
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_REQUEST_BYTES:
                return None
            if b"\n" in chunk:
                break
        return b"".join(chunks).partition(b"\n")[0]

    @staticmethod
    def _write(conn: socket.socket, response: dict) -> None:
        with contextlib.suppress(Exception):
            conn.sendall(json.dumps(response, default=str).encode("utf-8") + b"\n")

    # ── verbs ──────────────────────────────────────────────────────────────

    def handle_request_line(self, raw: bytes) -> dict:
        """One JSON request line -> one response dict. Never raises."""
        request_id: Any = None
        try:
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            request_id = request.get("id")
            verb = request.get("verb")
            params = request.get("params")
            params = params if isinstance(params, dict) else {}
            if verb == "identify":
                response = {"ok": True, "protocol": DELIVERY_PROTOCOL_VERSION,
                            "result": self._identify()}
            elif verb == "ticket.deliver":
                ok, payload = self._deliver(params)
                response = ({"ok": True, "protocol": DELIVERY_PROTOCOL_VERSION, "result": payload}
                            if ok else {"ok": False, "protocol": DELIVERY_PROTOCOL_VERSION, **payload})
            else:
                response = {"ok": False, "protocol": DELIVERY_PROTOCOL_VERSION,
                            "error": f"unknown verb: {verb!r}",
                            "supported_verbs": ["identify", "ticket.deliver"]}
        except Exception as exc:
            response = {"ok": False, "protocol": DELIVERY_PROTOCOL_VERSION,
                        "error": f"{type(exc).__name__}: {exc}"}
        if request_id is not None:
            response["id"] = request_id
        return response

    def _identify(self) -> dict:
        return {"protocol": DELIVERY_PROTOCOL_VERSION, "pid": os.getpid(),
                "hermes_home": str(self._home), "answered_at": time.time(),
                "sessions": _live_session_ids()}

    def _deliver(self, params: dict) -> tuple[bool, dict]:
        """``ticket.deliver`` → ``prompt.submit(queued=True)``.

        ``queued=True`` is the whole delivery contract: the message runs as the NEXT turn and
        never steers or interrupts one in flight (``session_auto_continue._handle_busy_submit``
        forces queue mode for it). No transport is bound for this call, so the live client keeps
        sole write ownership of the session.
        """
        from tui_gateway import server

        text = params.get("text")
        text = text.strip() if isinstance(text, str) else ""
        if not text:
            return False, {"error": "text required"}
        target, error = _resolve_target(str(params.get("session_id") or "").strip())
        if error:
            return False, {"error": error}
        submit = server._methods.get("prompt.submit")
        if submit is None:
            return False, {"error": "prompt.submit unavailable"}
        with self._deliver_lock:
            response = submit(None, {"session_id": target, "text": text, "queued": True})
        if isinstance(response, dict) and response.get("error"):
            return False, {"error": str((response["error"] or {}).get("message", "delivery refused")),
                           "code": (response["error"] or {}).get("code")}
        result = response.get("result") if isinstance(response, dict) else {}
        return True, {"delivered": True, "session_id": target,
                      "status": (result or {}).get("status", "queued")}


def start_delivery_socket(home: Path | None = None) -> Optional[DeliverySocketServer]:
    """Start the delivery socket when configured; None when disabled or the bind failed.

    Never fatal: a gateway that cannot open this door still serves its own client.
    """
    if not delivery_socket_enabled():
        return None
    if home is None:
        from tui_gateway import server
        home = Path(server._hermes_home)
    server_obj = DeliverySocketServer(Path(home))
    return server_obj if server_obj.start() else None


def deliver_ticket(home: Path, text: str, *, session_id: str = "",
                   timeout: float = 10.0) -> dict[str, Any]:
    """Client side: hand ``text`` to the live session served by ``home``.

    Returns the server's response dict, or ``{"ok": False, "error": ...}`` when no socket answers
    (gateway not running, delivery socket disabled, permissions). Never raises.
    """
    from gateway.control_socket import resolve_client_socket_path_for

    path = resolve_client_socket_path_for(Path(home), DELIVERY_SOCKET_FILENAME)
    if path is None:
        return {"ok": False, "error": "no delivery socket for this HERMES_HOME"}
    request = json.dumps({"verb": "ticket.deliver", "id": 1,
                          "protocol": DELIVERY_PROTOCOL_VERSION,
                          "params": {"session_id": session_id, "text": text}}).encode("utf-8") + b"\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(path))
            sock.sendall(request)
            chunks: list[bytes] = []
            while b"\n" not in b"".join(chunks):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        raw = b"".join(chunks).partition(b"\n")[0]
        response = json.loads(raw.decode("utf-8")) if raw else None
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return response if isinstance(response, dict) else {"ok": False, "error": "malformed response"}

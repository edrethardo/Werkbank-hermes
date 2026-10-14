"""HTTP side of the ``werkbank`` plugin: the contract of ``PROTOCOL.md`` on ``127.0.0.1`` only.

Every request needs ``Authorization: Bearer <key>`` (``hmac.compare_digest``), else 401 before
anything else is looked at. The handler writes no access log; log lines of this module carry a
status or a reason, never a path with a query, a body, a chat text or the key.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import socket
import threading
import time
import urllib.parse
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional

from . import approvals, snapshot

logger = logging.getLogger("plugins.platforms.werkbank")

HOST = "127.0.0.1"                  # never configurable
DEFAULT_PORT = 8767                 # NOT 8765: that is the Werkbank board's own port
HEARTBEAT_S = 15.0
MAX_BODY = 512 * 1024
MAX_TEXT = 200_000
MAX_STREAMS = 32
MAX_CONNECTIONS = 64                 # connections not authenticated yet; beyond it the OLDEST one goes
MAX_AUTHENTICATED = MAX_STREAMS + 16 # authenticated connections (streams + requests): an own quota
HEADER_DEADLINE_S = 10.0             # request line + headers (+ POST body) must arrive within this
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")      # = the board's bruecke.ID_RE
_KEY_MAX = 512
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost")


def _loopback_host(header: Optional[str], port: int) -> bool:
    """``Host`` names this loopback server (DNS-rebinding defense in depth)."""
    if not header:
        return False
    host, _, given = header.strip().lower().rpartition(":")
    if not host or not given.isdigit():
        host, given = header.strip().lower(), ""
    return host in _LOOPBACK_HOSTS and given in ("", str(port))


def _hang_up(sock) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)          # its handler thread reads EOF and ends at once
    except OSError:
        pass


class _Server(ThreadingHTTPServer):
    """Two quotas, so a local flood cannot lock the board out:

    * connections that have not authenticated yet: at most ``max_pending``. When full, the OLDEST
      of them is hung up — never the new one — so a client that sends its headers at once always
      gets through, however fast an attacker reconnects;
    * authenticated connections: at most ``max_authenticated``, untouched by the first quota (a
      request beyond it gets 503).
    """

    daemon_threads = True
    allow_reuse_address = True
    max_pending = MAX_CONNECTIONS
    max_authenticated = MAX_AUTHENTICATED

    def __init__(self, *args, **kwargs):
        self._conn_lock = threading.Lock()
        self._pending: "OrderedDict[Any, None]" = OrderedDict()
        self._authed: set = set()
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        with self._conn_lock:
            victims = []
            while self._pending and len(self._pending) >= self.max_pending:
                victims.append(self._pending.popitem(last=False)[0])
            self._pending[request] = None
        for victim in victims:
            _hang_up(victim)
        try:
            super().process_request(request, client_address)
        except Exception:
            self._forget(request)
            raise

    def authenticate(self, request) -> bool:
        """Move *request* from the pending to the authenticated quota; False when that is full or
        the connection was already hung up."""
        with self._conn_lock:
            if request not in self._pending or len(self._authed) >= self.max_authenticated:
                return False
            del self._pending[request]
            self._authed.add(request)
            return True

    def _forget(self, request) -> None:
        with self._conn_lock:
            self._pending.pop(request, None)
            self._authed.discard(request)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._forget(request)

    def handle_error(self, request, client_address):
        # A client that hangs up (or was hung up) mid-answer is normal here: no traceback flood.
        import sys
        exc = sys.exc_info()[1]
        if not isinstance(exc, OSError):
            logger.warning("werkbank: request failed (%s)", type(exc).__name__)


class WerkbankServer:
    """``WerkbankServer(key, buffers, host, port).start()``; ``stop()`` on unload."""

    def __init__(self, key: str, buffers, host, port: int = DEFAULT_PORT, *, heartbeat: float = HEARTBEAT_S,
                 mirror_enabled: Callable[[], bool] = lambda: True, mirror_name: str = "",
                 max_streams: int = MAX_STREAMS,
                 max_connections: int = MAX_CONNECTIONS, max_authenticated: int = MAX_AUTHENTICATED,
                 header_deadline: float = HEADER_DEADLINE_S) -> None:
        if not key or any(c.isspace() for c in key):
            raise ValueError("unusable key")
        self._wanted = ("Bearer " + key).encode("utf-8")
        self.buffers = buffers
        self.host = host
        self.requested_port = int(port)
        self.heartbeat = heartbeat
        self.mirror_enabled = mirror_enabled
        self.mirror_name = mirror_name
        self.max_streams = max_streams
        self.max_connections = max_connections
        self.max_authenticated = max_authenticated
        self.header_deadline = header_deadline
        self.open_streams = 0
        self._streams_lock = threading.Lock()
        self._httpd: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None
        self._closing = threading.Event()

    # -- lifecycle ------------------------------------------------------------------------------
    @property
    def address(self):
        return self._httpd.server_address if self._httpd else None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def start(self) -> "WerkbankServer":
        self._httpd = _Server((HOST, self.requested_port), _handler(self))
        self._httpd.max_pending = self.max_connections
        self._httpd.max_authenticated = self.max_authenticated
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="werkbank-plugin-http",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._closing.set()
        self.buffers.close()
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()

    # -- helpers --------------------------------------------------------------------------------
    def authorized(self, header: Optional[str]) -> bool:
        return hmac.compare_digest((header or "").encode("utf-8", "replace"), self._wanted)

    def take_stream(self) -> bool:
        with self._streams_lock:
            if self.open_streams >= self.max_streams:
                return False
            self.open_streams += 1
            return True

    def give_stream(self) -> None:
        with self._streams_lock:
            self.open_streams -= 1

    @property
    def closing(self) -> bool:
        return self._closing.is_set()


def _valid_key(key: str) -> bool:
    return 0 < len(key) <= _KEY_MAX and key.isprintable()


def _handler(app: WerkbankServer):

    class Handler(BaseHTTPRequestHandler):
        server_version = "hermes-werkbank/1"
        timeout = 30                         # a stalled client never holds a thread forever
        sys_version = ""

        def log_message(self, fmt, *args):      # no access log: paths carry session keys and ids
            pass

        def log_error(self, fmt, *args):
            pass

        # -- total deadline for request line + headers (slowloris) ------------------------------
        def setup(self):
            super().setup()
            self._deadline = threading.Timer(app.header_deadline, self._expire)
            self._deadline.daemon = True
            self._deadline.start()

        def _expire(self):
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def _in_time(self) -> None:
            self._deadline.cancel()

        def handle(self):
            try:
                super().handle()
            except OSError:                       # BrokenPipe, reset, hung up by the quota: no traceback
                self.close_connection = True

        def finish(self):
            self._deadline.cancel()
            try:
                super().finish()
            except OSError:
                pass

        def parse_request(self):
            ok = super().parse_request()
            if ok and self.command != "POST":
                self._in_time()                  # a POST body still runs under the deadline
            return ok

        # -- answers --------------------------------------------------------------------------
        def _json(self, code: int, body: Dict[str, Any]) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _refuse(self, code: int, error: str, grund: str, **extra) -> None:
            self._json(code, {"error": error, "grund": grund, **extra})

        def _auth(self) -> bool:
            if not _loopback_host(self.headers.get("Host"), app.port):
                self._json(421, {"error": "misdirected request"})
                return False
            if not app.authorized(self.headers.get("Authorization")):
                self._json(401, {"error": "unauthorized"})
                return False
            httpd = app._httpd
            if httpd is not None and not httpd.authenticate(self.connection):
                self._json(503, {"error": "busy", "grund": "Zu viele offene Verbindungen zum Hermes-Plugin."})
                return False
            return True

        def _session(self, parts) -> Optional[str]:
            key = urllib.parse.unquote(parts[2])
            if not _valid_key(key) or app.buffers.get(key) is None:
                self._refuse(404, "unknown session", "Diese Sitzung ist nicht (mehr) aufgelöst.")
                return None
            return key

        # -- GET --------------------------------------------------------------------------------
        def do_GET(self):  # noqa: N802
            if not self._auth():
                return
            try:
                self._get()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                logger.warning("werkbank: GET failed (%s)", "internal", exc_info=False)
                try:
                    self._refuse(500, "internal", "Interner Fehler im Hermes-Plugin werkbank.")
                except Exception:
                    pass

        def _get(self):
            split = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(split.query)
            parts = split.path.split("/")[1:]
            if split.path == "/v1/resolve":
                return self._resolve((query.get("session_id") or [""])[0])
            if len(parts) >= 4 and parts[:2] == ["v1", "s"]:
                key = self._session(parts)
                if key is None:
                    return
                what = parts[3:]
                if what == ["snapshot"]:
                    body = snapshot.build(app.buffers, app.host, key)
                    if body is None:
                        return self._refuse(404, "unknown session", "Diese Sitzung ist nicht (mehr) aufgelöst.")
                    return self._json(200, body)
                if what == ["events"]:
                    return self._events(key, query)
                if len(what) == 3 and what[0] == "tools" and what[2] == "output":
                    return self._tool_output(key, urllib.parse.unquote(what[1]))
            self._refuse(404, "not found", "Unbekannter Pfad.")

        def _resolve(self, session_id: str):
            if not ID_RE.fullmatch(session_id or ""):
                return self._refuse(400, "bad session_id", "Ungültige session_id.")
            try:
                info = app.host.resolve(session_id)
            except RuntimeError:
                return self._refuse(503, "gateway not ready", "Das Gateway ist noch nicht bereit.")
            if info is None:
                key = app.buffers.key_for_session_id(session_id)
                current = app.host.current_session_id(key) if key else None
                if key and current:
                    buf = app.buffers.get(key)
                    info = {"session_key": key, "session_id": current,
                            "platform": buf.platform if buf else None, "live": True}
            if info is None:
                return self._refuse(404, "unknown session", "Das Gateway kennt diese Sitzung nicht.")
            app.buffers.watch(info["session_key"], info.get("session_id"), info.get("platform"))
            self._json(200, {"session_key": info["session_key"], "session_id": info.get("session_id"),
                             "platform": info.get("platform"), "live": bool(info.get("live"))})

        def _tool_output(self, key: str, call_id: str):
            if not ID_RE.fullmatch(call_id or ""):
                return self._refuse(400, "bad tool_call_id", "Ungültige Werkzeug-ID.")
            buf = app.buffers.get(key)
            output = app.host.tool_output(key, buf.session_id if buf else None, call_id)
            if output is None:
                return self._refuse(404, "unknown tool call", "Diese Ausgabe kennt Hermes nicht (mehr).")
            self._json(200, {"tool_call_id": call_id, "output": output})

        def _events(self, key: str, query):
            try:
                epoch = int((query.get("epoch") or ["0"])[0])
                after = int((query.get("after") or ["0"])[0])
            except ValueError:
                return self._refuse(400, "bad epoch/after", "epoch und after müssen Zahlen sein.")
            if epoch < 0 or after < 0:
                return self._refuse(400, "bad epoch/after", "epoch und after müssen Zahlen sein.")
            if not app.take_stream():
                return self._refuse(429, "too many streams", "Zu viele offene Ereignisströme.")
            app.buffers.reader(+1, key)
            try:
                self._stream(key, epoch, after)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                app.buffers.reader(-1, key)
                app.give_stream()

        def _frame(self, event) -> None:
            self.wfile.write(b"data: " + json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n\n")

        def _stream(self, key: str, epoch: int, after: int):
            buffers = app.buffers
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            with buffers.cond:                   # decide under the lock, write outside it
                buf = buffers.get(key)
                if buf is None:
                    return
                pending = None if epoch != buf.epoch else buf.after(after)
                first = (buffers.epoch_event(buf, "epoch" if epoch != buf.epoch else "after")
                         if pending is None else None)
                current = buf.epoch
            if first is not None:
                self._frame(first)
                self.wfile.flush()
                return
            sent = after
            last = time.monotonic()
            while not app.closing:
                with buffers.cond:
                    end = None
                    if buffers.closed:
                        return
                    if buf.evicted or buffers.get(key) is not buf:
                        # dropped from the buffers (LRU): the client reloads, re-resolves
                        end = buffers.epoch_event(buf, "neu")
                        pending = []
                    elif buf.epoch != current:
                        end = buffers.epoch_event(buf, "neu")
                        pending = []
                    else:
                        pending = buf.after(sent)
                        if pending is None:             # this reader fell out of the ring
                            end = buffers.epoch_event(buf, "after")
                            pending = []
                    if not pending and end is None:
                        buffers.cond.wait(timeout=min(app.heartbeat, 1.0))
                for event in pending:
                    self._frame(event)
                    sent = event["seq"]
                    last = time.monotonic()
                if end is not None:
                    self._frame(end)
                    self.wfile.flush()
                    return
                if time.monotonic() - last >= app.heartbeat:
                    self.wfile.write(b": hb\n\n")
                    last = time.monotonic()
                self.wfile.flush()

        # -- POST -------------------------------------------------------------------------------
        def do_POST(self):  # noqa: N802
            if not self._auth():
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._refuse(400, "bad length", "Ungültige Länge.")
            if length < 0 or length > MAX_BODY:
                return self._refuse(413, "too large", "Die Anfrage ist zu groß.")
            raw = self.rfile.read(length) if length else b""
            self._in_time()
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError):
                body = None
            if not isinstance(body, dict):
                return self._refuse(400, "bad body", "Erwartet ein JSON-Objekt.")
            try:
                self._post(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                logger.warning("werkbank: POST failed (%s)", "internal", exc_info=False)
                try:
                    self._refuse(500, "internal", "Interner Fehler im Hermes-Plugin werkbank.")
                except Exception:
                    pass

        def _post(self, body: Dict[str, Any]):
            parts = urllib.parse.urlsplit(self.path).path.split("/")[1:]
            if len(parts) < 4 or parts[:2] != ["v1", "s"]:
                return self._refuse(404, "not found", "Unbekannter Pfad.")
            key = self._session(parts)
            if key is None:
                return
            what = parts[3:]
            if what == ["messages"]:
                return self._message(key, body)
            if what == ["stop"]:
                return self._stop(key)
            if len(what) == 2 and what[0] in ("approvals", "clarify"):
                return self._answer(key, what[0], urllib.parse.unquote(what[1]), body)
            self._refuse(404, "not found", "Unbekannter Pfad.")

        def _answer(self, key: str, kind: str, item_id: str, body: Dict[str, Any]):
            """WB-1129: one approval / clarify prompt of THIS session, by its exact id."""
            if not ID_RE.fullmatch(item_id or ""):
                return self._refuse(400, "bad id", "Ungültige Anfrage-ID.")
            answer = approvals.answer_approval if kind == "approvals" else approvals.answer_clarify
            status, payload = answer(key, item_id, body)
            if status == 200:
                logger.info("werkbank: %s answered from the board (resolved=%s)", kind,
                            payload.get("resolved"))
            self._json(status, payload)

        def _message(self, key: str, body: Dict[str, Any]):
            text = body.get("text")
            cmid = body.get("client_message_id")
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
                return self._refuse(400, "bad text", "Bitte einen Text senden (höchstens 200 000 Zeichen).",
                                    accepted=False)
            if not isinstance(cmid, str) or not ID_RE.fullmatch(cmid):
                return self._refuse(400, "bad client_message_id", "Ungültige Nachrichten-ID.", accepted=False)
            awaits = not app.host.supports_client_message_id
            if not app.buffers.accept_id(key, cmid, awaits_event=awaits):
                return self._refuse(409, "duplicate", "Diese Nachricht wurde schon angenommen.",
                                    accepted=False)
            # WB-1148: the mirror goes out FIRST, so its Telegram message id can be the reply anchor of
            # Hermes' answer. A mirror that fails or is slow (host.MIRROR_WAIT_S) leaves no anchor.
            anchor = None
            if app.mirror_enabled():
                try:
                    anchor = app.host.mirror(key, text, app.mirror_name)
                except Exception:
                    logger.info("werkbank: mirror line not delivered")
            try:
                accepted, _ = app.host.inject(key, text, cmid, reply_to_message_id=anchor)
            except Exception:
                accepted = False
            if not accepted:
                app.buffers.release_id(key, cmid)
                if anchor is not None:
                    try:
                        app.host.unmirror(key, anchor)      # Hermes never got it: take the mirror back
                    except Exception:
                        logger.info("werkbank: mirror line not taken back")
                logger.info("werkbank: message refused by the gateway (session known, text not logged)")
                return self._refuse(503, "not accepted",
                                    "Hermes hat die Nachricht nicht angenommen (Gateway nicht bereit oder "
                                    "allow_gateway_injection fehlt).", accepted=False)
            self._json(200, {"accepted": True})

        def _stop(self, key: str):
            result = app.host.interrupt(key)
            if result is None:
                return self._refuse(501, "not implemented",
                                    "Dieses Hermes kann keinen Plugin-Stopp (PluginContext.interrupt_session "
                                    "fehlt, WB-1127).", stopped=False)
            self._json(200, {"stopped": bool(result)})

    return Handler

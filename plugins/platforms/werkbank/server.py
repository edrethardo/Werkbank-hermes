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
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional

from . import snapshot

logger = logging.getLogger("plugins.platforms.werkbank")

HOST = "127.0.0.1"                  # never configurable
DEFAULT_PORT = 8767                 # NOT 8765: that is the Werkbank board's own port
HEARTBEAT_S = 15.0
MAX_BODY = 512 * 1024
MAX_TEXT = 200_000
MAX_STREAMS = 32
MIRROR_PREFIX = "aus Werkbank: "
MIRROR_MAX = 3500                    # a Telegram message holds 4096 characters
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")      # = the board's bruecke.ID_RE
_KEY_MAX = 512


def mirror_line(text: str) -> str:
    body = text.strip()
    if len(body) > MIRROR_MAX:
        body = body[:MIRROR_MAX].rstrip() + " …"
    return MIRROR_PREFIX + body


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class WerkbankServer:
    """``WerkbankServer(key, buffers, host, port).start()``; ``stop()`` on unload."""

    def __init__(self, key: str, buffers, host, port: int = DEFAULT_PORT, *, heartbeat: float = HEARTBEAT_S,
                 mirror_enabled: Callable[[], bool] = lambda: True, max_streams: int = MAX_STREAMS,
                 idmap=None) -> None:
        if not key or any(c.isspace() for c in key):
            raise ValueError("unusable key")
        self._wanted = ("Bearer " + key).encode("utf-8")
        self.buffers = buffers
        self.host = host
        self.requested_port = int(port)
        self.heartbeat = heartbeat
        self.mirror_enabled = mirror_enabled
        self.max_streams = max_streams
        if idmap is None:
            from .idmap import IdMap
            idmap = IdMap()
        self.idmap = idmap
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
            if app.authorized(self.headers.get("Authorization")):
                return True
            self._json(401, {"error": "unauthorized"})
            return False

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
                    body = snapshot.build(app.buffers, app.host, key, app.idmap)
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
            try:
                self._stream(key, epoch, after)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
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
            with buffers.cond:
                buf = buffers.get(key)
                pending = None if buf is None or epoch != buf.epoch else buf.after(after)
                if buf is None:
                    return
                if pending is None:
                    self._frame(buffers.epoch_event(buf, "epoch" if epoch != buf.epoch else "after"))
                    self.wfile.flush()
                    return
                current = buf.epoch
            sent = after
            last = time.monotonic()
            while not app.closing:
                with buffers.cond:
                    end = None
                    if buffers.get(key) is not buf or buffers.closed:
                        return
                    if buf.epoch != current:
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
                # WB-1129 (plan: Kind 4) answers these; the contract route exists already.
                return self._refuse(501, "not implemented",
                                    "Freigaben und Rückfragen aus der Werkbank kommen mit WB-1129.")
            self._refuse(404, "not found", "Unbekannter Pfad.")

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
            app.idmap.remember(key, text, cmid)       # before the turn can persist its row
            try:
                accepted, _ = app.host.inject(key, text, cmid)
            except Exception:
                accepted = False
            if not accepted:
                app.buffers.release_id(key, cmid)
                app.idmap.forget(key, cmid)
                logger.info("werkbank: message refused by the gateway (session known, text not logged)")
                return self._refuse(503, "not accepted",
                                    "Hermes hat die Nachricht nicht angenommen (Gateway nicht bereit oder "
                                    "allow_gateway_injection fehlt).", accepted=False)
            if app.mirror_enabled():
                try:
                    app.host.mirror(key, mirror_line(text))
                except Exception:
                    logger.info("werkbank: mirror line not scheduled")
            self._json(200, {"accepted": True})

        def _stop(self, key: str):
            result = app.host.interrupt(key)
            if result is None:
                return self._refuse(501, "not implemented",
                                    "Dieses Hermes kann keinen Plugin-Stopp (PluginContext.interrupt_session "
                                    "fehlt, WB-1127).", stopped=False)
            self._json(200, {"stopped": bool(result)})

    return Handler

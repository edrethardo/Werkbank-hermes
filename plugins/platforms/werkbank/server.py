"""HTTP side of the ``werkbank`` plugin: the contract of ``PROTOCOL.md`` on ``127.0.0.1`` only.

Every request needs ``Authorization: Bearer <key>`` (``hmac.compare_digest``), else 401 before
anything else is looked at. The handler writes no access log; log lines of this module carry a
status or a reason, never a path with a query, a body, a chat text or the key.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.parse
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional

from . import approvals, snapshot
from . import commands as commands_mod
from . import mirror as mirror_mod
from . import outbox as outbox_mod
from . import subagents as subagents_mod
from . import uploads as uploads_mod

logger = logging.getLogger("plugins.platforms.werkbank")

HOST = "127.0.0.1"                  # never configurable
DEFAULT_PORT = 8767                 # NOT 8765: that is the Werkbank board's own port
HEARTBEAT_S = 15.0
MAX_BODY = 512 * 1024
MAX_UPLOAD_BODY = 28 * 1024 * 1024  # WB-1174: one file of uploads.MAX_FILE as base64 in JSON, plus room
MAX_TEXT = 200_000
NEW_TITLE_MAX = 60                  # WB-1342: title of a new chat (a Telegram topic name)
MAX_STREAMS = 32
MAX_FILE_TRANSFERS = 16              # WB-1321: outbox file transfers at once; beyond it 503 + Retry-After
FILE_RETRY_AFTER_S = 2
MAX_CONNECTIONS = 64                 # connections not authenticated yet; beyond it the OLDEST one goes
# authenticated connections: streams + file transfers + 16 for snapshots and other requests
MAX_AUTHENTICATED = MAX_STREAMS + MAX_FILE_TRANSFERS + 16
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
                 header_deadline: float = HEADER_DEADLINE_S, uploads: Any = None, outbox: Any = None) -> None:
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
        self.uploads = uploads              # WB-1174: uploads.Uploads, None = attachments off
        self.outbox = outbox                # WB-1321: outbox.Outbox, None = Hermes' files off
        if outbox is not None and getattr(buffers, "outbox", None) is None:
            buffers.outbox = outbox
        self.open_streams = 0
        self._streams_lock = threading.Lock()
        # WB-1321 Härtung: byte transfers of outbox files (originals, versions, HLS) have their own
        # quota inside the authenticated one, so streams and snapshots always keep room
        self.max_file_transfers = MAX_FILE_TRANSFERS
        self.open_file_transfers = 0
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

    def take_file_transfer(self) -> bool:
        with self._streams_lock:
            if self.open_file_transfers >= self.max_file_transfers:
                return False
            self.open_file_transfers += 1
            return True

    def give_file_transfer(self) -> None:
        with self._streams_lock:
            self.open_file_transfers = max(0, self.open_file_transfers - 1)

    @property
    def closing(self) -> bool:
        return self._closing.is_set()


def _secret_intake(key: str, text: str) -> str:
    """The board text with every secret replaced (``agent/secret_intake.py``); unchanged when the core
    has no intake (older Hermes) or ``security.secret_intake`` is off."""
    try:
        from agent import secret_intake
    except Exception:
        return text
    try:
        return secret_intake.intake(text, key, surface="werkbank").text
    except Exception:
        logger.warning("werkbank: secret intake failed (text not logged)", exc_info=False)
        return text


def with_attachments(uploads, key: str, event: Dict[str, Any]) -> Dict[str, Any]:
    """WB-1174: a ``user.message`` of a board message with attachments gets ``payload.attachments``
    (a copy; the buffered event stays as the core sent it)."""
    if uploads is None or event.get("type") != "user.message":
        return event
    payload = event.get("payload") or {}
    found = uploads.attachments(key, payload.get("client_message_id"))
    if not found:
        return event
    return {**event, "payload": {**payload, "attachments": found}}


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
            if split.path == "/v1/commands":             # WB-1256: the board's completion list
                return self._json(200, {"commands": commands_mod.catalog()})
            if len(parts) >= 4 and parts[:2] == ["v1", "s"]:
                key = self._session(parts)
                if key is None:
                    return
                what = parts[3:]
                if what == ["snapshot"]:
                    body = snapshot.build(app.buffers, app.host, key)
                    if body is None:
                        return self._refuse(404, "unknown session", "Diese Sitzung ist nicht (mehr) aufgelöst.")
                    if app.uploads is not None:
                        for message in body.get("messages") or []:
                            found = app.uploads.attachments(key, message.get("client_message_id"))
                            if found:
                                message["attachments"] = found
                    if app.outbox is not None:           # WB-1321: Hermes' files, tags out of the text
                        app.outbox.dress(key, body.get("messages") or [])
                    return self._json(200, body)
                if what == ["events"]:
                    return self._events(key, query)
                if len(what) == 3 and what[0] == "tools" and what[2] == "output":
                    return self._tool_output(key, urllib.parse.unquote(what[1]))
                if len(what) == 2 and what[0] == "uploads":
                    return self._upload_file(key, urllib.parse.unquote(what[1]))
                if len(what) in (2, 3) and what[0] == "files":          # WB-1321
                    return self._outbox_file(key, what[1], what[2] if len(what) == 3 else None)
                if len(what) == 4 and what[0] == "files" and what[2] == "hls":      # WB-1321: HLS
                    return self._outbox_file(key, what[1], "hls", hls_name=what[3])
            self._refuse(404, "not found", "Unbekannter Pfad.")

        def _outbox_file(self, key: str, fid: str, variant: Optional[str], hls_name: Optional[str] = None):
            """WB-1321: a file Hermes sent in THIS session — its metadata, or the bytes of one variant
            (``original`` or a ready playback version, or an HLS playlist/segment) with single-range
            support (206/416)."""
            if app.outbox is None:
                return self._refuse(501, "no outbox", "Dateiablage ist nicht eingerichtet.")
            if not outbox_mod.ID_RE.fullmatch(fid or ""):
                return self._refuse(400, "bad file id", "Ungültige Datei-ID.")
            if variant is None:
                meta = app.outbox.get(key, fid)
                if meta is None:
                    return self._refuse(404, "unknown file", "Diese Datei gibt es (nicht mehr).")
                return self._json(200, app.outbox.public(meta))
            if variant == "hls":
                if not outbox_mod.HLS_NAME_RE.fullmatch(hls_name or ""):
                    return self._refuse(400, "bad hls name", "Unbekannter HLS-Teil.")
                found = app.outbox.hls_path(key, fid, hls_name)
            elif variant != "original" and variant not in outbox_mod.VARIANTS:
                return self._refuse(400, "bad variant", "Unbekannte Fassung.")
            else:
                found = app.outbox.path(key, fid, variant)
            if found is None:
                return self._refuse(404, "unknown file", "Diese Datei oder Fassung gibt es (noch) nicht.")
            path, media_type = found
            if not app.take_file_transfer():
                self.send_response(503)
                data = json.dumps({"error": "busy", "grund": "Zu viele Datei-Übertragungen gleichzeitig."}).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Retry-After", str(FILE_RETRY_AFTER_S))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            try:
                self._send_file(path, media_type)
            finally:
                app.give_file_transfer()

        def _send_file(self, path, media_type: str) -> None:
            try:
                handle = open(path, "rb")
            except OSError:
                return self._refuse(404, "unknown file", "Diese Datei gibt es (nicht mehr).")
            with handle:
                size = os.fstat(handle.fileno()).st_size
                wanted = outbox_mod.parse_range(self.headers.get("Range"), size)
                if wanted == "416":
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start, end = wanted if isinstance(wanted, tuple) else (0, size - 1)
                self.send_response(206 if isinstance(wanted, tuple) else 200)
                self.send_header("Content-Type", media_type)
                self.send_header("Accept-Ranges", "bytes")
                if isinstance(wanted, tuple):
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(max(0, end - start + 1)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                handle.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = handle.read(min(outbox_mod.CHUNK, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

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

        def _upload_file(self, key: str, upload_id: str):
            """WB-1174: an uploaded file of THIS session (the board's thumbnail). An image as its own
            type, everything else as ``application/octet-stream``; ``nosniff`` always."""
            found = app.uploads.file(key, upload_id) if app.uploads is not None else None
            if found is None:
                return self._refuse(404, "unknown upload", "Diesen Anhang gibt es (nicht mehr).")
            path, media_type, _name, kind = found
            try:
                with open(path, "rb") as handle:
                    data = handle.read(uploads_mod.MAX_FILE + 1)
            except OSError:
                return self._refuse(404, "unknown upload", "Diesen Anhang gibt es (nicht mehr).")
            self.send_response(200)
            self.send_header("Content-Type", media_type if kind == uploads_mod.IMAGE else "application/octet-stream")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

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
                    self._frame(with_attachments(app.uploads, key, event))
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
            limit = MAX_UPLOAD_BODY if urllib.parse.urlsplit(self.path).path.endswith("/uploads") else MAX_BODY
            if length < 0 or length > limit:
                self.close_connection = True       # the body is never read
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
            if parts == ["v1", "sessions"]:                  # WB-1342: a new chat for a new project
                return self._new_session(body)
            if len(parts) < 4 or parts[:2] != ["v1", "s"]:
                return self._refuse(404, "not found", "Unbekannter Pfad.")
            key = self._session(parts)
            if key is None:
                return
            what = parts[3:]
            if what == ["messages"]:
                return self._message(key, body)
            if what == ["uploads"]:
                return self._upload(key, body)
            if what == ["stop"]:
                return self._stop(key)
            if len(what) == 2 and what[0] in ("approvals", "clarify"):
                return self._answer(key, what[0], urllib.parse.unquote(what[1]), body)
            if len(what) == 2 and what[0] == "choices":    # WB-1256: a card a command opened
                status, payload = commands_mod.answer_choice(key, urllib.parse.unquote(what[1]), body)
                if status == 200:
                    logger.info("werkbank: choice card answered from the board")
                return self._json(status, payload)
            if what == ["subagents", "steer"]:              # WB-1338: write to a running child
                buf = app.buffers.get(key)
                ids = {getattr(buf, "session_id", None), app.host.current_session_id(key)}
                status, payload = subagents_mod.steer(ids, body)
                logger.info("werkbank: subagent steer from the board (status=%s)", status)
                return self._json(status, payload)
            self._refuse(404, "not found", "Unbekannter Pfad.")

        def _new_session(self, body: Dict[str, Any]):
            """WB-1342: ``POST /v1/sessions {next_to, title, text}`` — a new chat (sibling thread +
            session) next to the chat of the session ``next_to``, its first turn started with *text*
            (``host.new_session``). The text is never logged."""
            next_to, title, text = body.get("next_to"), body.get("title"), body.get("text")
            if not isinstance(next_to, str) or not _valid_key(next_to):
                return self._refuse(400, "bad next_to", "Ungültige Vorlage-Sitzung.")
            if (not isinstance(title, str) or not title.strip() or len(title) > NEW_TITLE_MAX
                    or not title.isprintable()):
                return self._refuse(400, "bad title", "Bitte einen einzeiligen Titel (höchstens 60 Zeichen).")
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
                return self._refuse(400, "bad text", "Bitte eine Startanweisung senden (höchstens 200 000 Zeichen).")
            create = getattr(app.host, "new_session", None)
            if not callable(create):
                return self._refuse(501, "not implemented", "Dieses Hermes-Plugin legt keine neuen Chats an.")
            from .host import NewSessionRefused
            try:
                out = create(next_to, title.strip(), text)
            except NewSessionRefused as e:
                logger.info("werkbank: new chat refused (status=%s)", e.status)
                extra = {k: e.extra[k] for k in ("angelegt", "session_id", "session_key", "thread_id")
                         if k in e.extra}
                return self._refuse(e.status, "refused", e.grund, **extra)
            self._json(200, {"session_key": str(out["session_key"]), "session_id": str(out["session_id"]),
                             "thread_id": str(out.get("thread_id") or "")})

        # -- DELETE -----------------------------------------------------------------------------
        def do_DELETE(self):  # noqa: N802
            """WB-1174: ``DELETE /v1/s/{key}/uploads/{id}`` — an unsent attachment of THIS session
            goes (the board chip's ✕). No body; a sent one stays (409)."""
            if not self._auth():
                return
            if (self.headers.get("Content-Length") or "0").strip() != "0" or self.headers.get("Transfer-Encoding"):
                self.close_connection = True       # the body is never read
                return self._refuse(400, "body not allowed", "DELETE ohne Inhalt.")
            self._in_time()
            try:
                parts = urllib.parse.urlsplit(self.path).path.split("/")[1:]
                if len(parts) != 5 or parts[:2] != ["v1", "s"] or parts[3] != "uploads":
                    return self._refuse(404, "not found", "Unbekannter Pfad.")
                key = self._session(parts)
                if key is None:
                    return
                upload_id = urllib.parse.unquote(parts[4])
                if not uploads_mod.ID_RE.fullmatch(upload_id):
                    return self._refuse(400, "bad upload id", "Ungültige Anhang-ID.")
                state = app.uploads.discard(key, upload_id) if app.uploads is not None else "unknown"
                if state == "bound":
                    return self._refuse(409, "already sent", "Dieser Anhang ist schon gesendet und bleibt.")
                if state != "deleted":
                    return self._refuse(404, "unknown upload", "Diesen Anhang gibt es (nicht mehr).")
                logger.info("werkbank: unsent attachment removed")
                self._json(200, {"deleted": upload_id})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                logger.warning("werkbank: DELETE failed (%s)", "internal", exc_info=False)
                try:
                    self._refuse(500, "internal", "Interner Fehler im Hermes-Plugin werkbank.")
                except Exception:
                    pass

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

        def _upload(self, key: str, body: Dict[str, Any]):
            """WB-1174: one file for a later message (``uploads.py``)."""
            if app.uploads is None:
                return self._refuse(501, "not implemented", "Anhänge sind in diesem Hermes-Plugin nicht eingerichtet.")
            raw = body.get("data")
            try:
                if not isinstance(raw, str):
                    raise ValueError
                data = base64.b64decode(raw, validate=True)
            except (ValueError, binascii.Error):
                return self._refuse(400, "bad data", "Die Datei muss als base64 kommen.")
            try:
                rec = app.uploads.store(key, body.get("name"), data)
            except uploads_mod.UploadError as e:
                return self._refuse(e.status, e.error, e.grund)
            logger.info("werkbank: attachment stored (%s, %d bytes)", rec["kind"], rec["size"])
            self._json(200, {"upload_id": rec["id"], "name": rec["name"], "type": rec["type"],
                             "size": rec["size"], "kind": rec["kind"]})

        def _message(self, key: str, body: Dict[str, Any]):
            text = body.get("text")
            cmid = body.get("client_message_id")
            ids = body.get("attachments")
            has_files = ids is not None
            if (not isinstance(text, str) or len(text) > MAX_TEXT
                    or (not has_files and not text.strip())):
                return self._refuse(400, "bad text", "Bitte einen Text senden (höchstens 200 000 Zeichen).",
                                    accepted=False)
            if not isinstance(cmid, str) or not ID_RE.fullmatch(cmid):
                return self._refuse(400, "bad client_message_id", "Ungültige Nachrichten-ID.", accepted=False)
            # WB-1256: a slash command goes to the gateway's command path — never to the model as text.
            command = commands_mod.is_command(text)
            if command and has_files:
                return self._refuse(400, "command with attachments",
                                    "Ein Befehl nimmt keine Anhänge mit — erst den Befehl, dann die Datei.",
                                    accepted=False)
            if command and not getattr(app.host, "supports_commands", False):
                return self._refuse(501, "not implemented",
                                    "Dieses Hermes führt Befehle aus dem Board noch nicht aus (inject_message ohne "
                                    "as_command, WB-1256) — der Befehl wurde NICHT als Text gesendet.",
                                    accepted=False)
            if has_files and (app.uploads is None or not getattr(app.host, "supports_media", False)):
                return self._refuse(501, "not implemented",
                                    "Dieses Hermes nimmt noch keine Anhänge an (inject_message ohne media_urls, "
                                    "WB-1174) — der Text allein geht.", accepted=False)
            awaits = not app.host.supports_client_message_id
            if not app.buffers.accept_id(key, cmid, awaits_event=awaits):
                return self._refuse(409, "duplicate", "Diese Nachricht wurde schon angenommen.",
                                    accepted=False)
            files = []
            if has_files:
                try:
                    files = app.uploads.bind(key, cmid, ids)
                except uploads_mod.UploadError as e:
                    app.buffers.release_id(key, cmid)
                    return self._refuse(e.status, e.error, e.grund, accepted=False)
            # WB-1212: secrets become placeholders before the mirror, the inject and the event stream see
            # the text; the gateway offers the vault card for them (same session key, not yet offered).
            text = _secret_intake(key, text)
            # WB-1148: the mirror goes out FIRST, so its Telegram message id can be the reply anchor of
            # Hermes' answer — and only when the gateway can take the message at all (host pre-check).
            # A mirror that fails or is slow (host.MIRROR_WAIT_S) leaves no anchor.
            posted = None
            if app.mirror_enabled() and not command:     # a command is no message for the topic
                try:
                    posted = app.host.mirror(key, mirror_mod.with_attachments(text, files), app.mirror_name)
                except Exception:
                    logger.info("werkbank: mirror line not delivered")
            anchor = getattr(posted, "message_id", None)
            extra = {"media": [(f["path"], f["type"]) for f in files]} if files else {}
            if command:
                extra["command"] = True
            try:
                accepted, _ = app.host.inject(key, text, cmid, reply_to_message_id=anchor, **extra)
            except Exception:
                accepted = False
            if accepted and files and app.mirror_enabled():
                photos = [f["path"] for f in files if f["kind"] == uploads_mod.IMAGE]
                if photos:
                    try:
                        app.host.mirror_photos(key, photos, anchor)
                    except Exception:
                        logger.info("werkbank: mirror photos not sent")
            if not accepted:
                if files:
                    app.uploads.release(key, cmid)
                app.buffers.release_id(key, cmid)
                if posted is not None:
                    try:
                        posted.take_back()      # Hermes never got it: delete the mirror, now or once it lands
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

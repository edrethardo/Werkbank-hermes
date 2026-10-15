"""Attachments the Werkbank board uploads for a message (WB-1174).

One file per ``POST /v1/s/{key}/uploads``; the board binds the returned ids to a message
(``POST …/messages {attachments}``) and the plugin hands their paths to the turn
(``inject_message(media_urls=…, media_types=…)``) — the way a photo or document sent in Telegram reaches
the agent.

Where: the media cache of THIS plugin's profile (``<home>/cache/images`` / ``cache/documents``,
``hermes_constants.get_hermes_dir`` like the gateway's own cache accessors) — the place Telegram
downloads land, owner-only (directory 0700, file 0600, created with ``O_EXCL``), pruned hourly by the
gateway's housekeeping (older than 24 h). Never in a project, never in the board's state.

What: the bytes decide, never the name or a declared type (``sniff``). Images JPEG/PNG/GIF/WebP go to
the turn as images; HEIC/HEIF as a file (``application/octet-stream``: the vision path cannot decode it
without ``pillow_heif``); PDF, office documents (ZIP + their extension) and UTF-8 text as documents.
Anything else — SVG, executables, archives — is refused.

The index (id → file, message → ids) lives in this process only: after a gateway restart a message
shows its ``media_count`` again, nothing more. Unbound uploads go after ``OPEN_TTL_S``.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

MAX_FILE = 20 * 1024 * 1024        # bytes of one file
MAX_PER_MESSAGE = 10
MAX_OPEN = 20                      # uploads of one session not bound to a message yet
OPEN_TTL_S = 3600                  # an unbound upload is deleted after this
KEEP_S = 24 * 3600                 # = the gateway's media cache age (MEDIA_CACHE_MAX_AGE_HOURS)
MAX_INDEX = 500                    # uploads kept in the index (oldest bound ones are forgotten first)
NAME_MAX = 80
ID_RE = re.compile(r"up_[0-9a-f]{24}")

IMAGE, DOCUMENT = "image", "document"
_IMAGES = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
_HEIF_BRANDS = (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1")
_OFFICE = {".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
           ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
           ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}
_TEXT = {".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv", ".json": "application/json",
         ".log": "text/plain", ".yaml": "application/yaml", ".yml": "application/yaml"}


class UploadError(Exception):
    """A refusal the board may show: HTTP status, short error code, German reason."""

    def __init__(self, status: int, error: str, grund: str) -> None:
        super().__init__(grund)
        self.status, self.error, self.grund = status, error, grund


def _ext(name: str) -> str:
    return os.path.splitext(name or "")[1].lower()


def sniff(data: bytes, name: str = "") -> Optional[Tuple[str, str, str]]:
    """``(kind, media type for Hermes, extension)`` from the magic bytes, or ``None`` (refused)."""
    head = bytes(data[:64])
    if head.startswith(b"\xff\xd8\xff"):
        return IMAGE, "image/jpeg", ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return IMAGE, "image/png", ".png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return IMAGE, "image/gif", ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return IMAGE, "image/webp", ".webp"
    if head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS:
        return DOCUMENT, "application/octet-stream", ".heic"
    if head.startswith(b"%PDF-"):
        return DOCUMENT, "application/pdf", ".pdf"
    ext = _ext(name)
    if head.startswith(b"PK\x03\x04") and ext in _OFFICE:
        return DOCUMENT, _OFFICE[ext], ext
    if ext in _TEXT and b"\x00" not in data[:65536]:
        try:
            bytes(data[:65536]).decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.start < len(data[:65536]) - 3:       # a cut multi-byte character at the end is fine
                return None
        return DOCUMENT, _TEXT[ext], ext
    return None


def clean_name(name: Any, ext: str) -> str:
    """A display/file name: base name only, no control/format (bidi, zero-width) characters, only
    letters, digits, space and ``._()-``, at most ``NAME_MAX`` characters, ending in *ext*."""
    raw = name if isinstance(name, str) else ""
    raw = unicodedata.normalize("NFC", raw).replace("\\", "/").rsplit("/", 1)[-1]
    raw = "".join(ch for ch in raw if unicodedata.category(ch)[0] != "C")
    stem = os.path.splitext(raw)[0] if _ext(raw) else raw
    stem = re.sub(r"[^\w .()-]", "_", stem, flags=re.UNICODE)
    stem = re.sub(r"\s+", " ", stem).strip(" ._")
    stem = stem[:NAME_MAX - len(ext)].rstrip(" ._") or "anhang"
    return stem + ext


def _secure_dir(folder: Path) -> None:
    try:
        from gateway.platforms.base import _secure_media_cache_dir   # the gateway's own policy (managed installs)
        _secure_media_cache_dir(folder)
    except Exception:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not folder.is_dir():
        raise UploadError(500, "internal", "Der Medien-Cache ist nicht anlegbar.")


def _cache_dir(home: Path, kind: str) -> Path:
    new, old = ("cache/images", "image_cache") if kind == IMAGE else ("cache/documents", "document_cache")
    try:
        from hermes_constants import get_hermes_dir
        return Path(get_hermes_dir(new, old, home=Path(home)))
    except Exception:
        return Path(home) / new


def _write(folder: Path, filename: str, data: bytes) -> Path:
    path = folder / filename
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return path


class Uploads:
    """The plugin's upload index (thread-safe; the HTTP server is threaded)."""

    def __init__(self, home: Path, clock: Callable[[], float] = time.time) -> None:
        self.home = Path(home)
        self.clock = clock
        self._lock = threading.Lock()
        self._items: Dict[str, Dict[str, Any]] = {}        # id -> record (insertion order = age)
        self._bound: Dict[Tuple[str, str], List[str]] = {}  # (session_key, client_message_id) -> ids

    @staticmethod
    def public(rec: Dict[str, Any]) -> Dict[str, Any]:
        return {"id": rec["id"], "name": rec["name"], "type": rec["type"], "size": rec["size"],
                "kind": rec["kind"]}

    # -- store ---------------------------------------------------------------------------------
    def store(self, session_key: str, name: Any, data: bytes) -> Dict[str, Any]:
        if len(data) > MAX_FILE:
            raise UploadError(413, "too large", f"Die Datei ist größer als {MAX_FILE // (1024 * 1024)} MB.")
        if not data:
            raise UploadError(400, "empty", "Die Datei ist leer.")
        found = sniff(data, name if isinstance(name, str) else "")
        if found is None:
            raise UploadError(415, "unsupported type",
                              "Dieser Dateityp geht nicht (Bilder, PDF, Office, Text).")
        kind, media_type, ext = found
        self.sweep()
        with self._lock:
            open_ = sum(1 for r in self._items.values() if r["session_key"] == session_key and not r["bound"])
            if open_ >= MAX_OPEN:
                raise UploadError(429, "too many uploads",
                                  "Zu viele noch nicht gesendete Anhänge — erst senden oder entfernen.")
        shown = clean_name(name, ext)
        upload_id = "up_" + secrets.token_hex(12)
        folder = _cache_dir(self.home, kind)
        _secure_dir(folder)
        filename = (f"img_{upload_id[3:15]}{ext}" if kind == IMAGE
                    else f"doc_{upload_id[3:15]}_{shown}")      # doc_<id>_<name>: the gateway shows <name>
        path = _write(folder, filename, data)
        rec = {"id": upload_id, "session_key": session_key, "name": shown, "type": media_type,
               "size": len(data), "kind": kind, "path": str(path), "created": self.clock(), "bound": None}
        with self._lock:
            self._items[upload_id] = rec
            self._trim()
        return self.public(rec)

    def _trim(self) -> None:
        while len(self._items) > MAX_INDEX:
            victim = next((i for i, r in self._items.items() if r["bound"]), next(iter(self._items)))
            self._forget(victim, delete=not self._items[victim]["bound"])

    def _forget(self, upload_id: str, *, delete: bool) -> None:
        rec = self._items.pop(upload_id, None)
        if rec is None:
            return
        if rec["bound"]:
            ids = self._bound.get((rec["session_key"], rec["bound"]))
            if ids and upload_id in ids:
                ids.remove(upload_id)
                if not ids:
                    del self._bound[(rec["session_key"], rec["bound"])]
        if delete:
            try:
                os.unlink(rec["path"])
            except OSError:
                pass

    def sweep(self) -> None:
        """Delete unbound uploads older than ``OPEN_TTL_S``; forget entries whose file is gone or older
        than ``KEEP_S`` (the gateway deletes those files itself)."""
        now = self.clock()
        with self._lock:
            for upload_id, rec in list(self._items.items()):
                age = now - rec["created"]
                if not rec["bound"] and age > OPEN_TTL_S:
                    self._forget(upload_id, delete=True)
                elif age > KEEP_S or not os.path.exists(rec["path"]):
                    self._forget(upload_id, delete=False)

    # -- read ----------------------------------------------------------------------------------
    def file(self, session_key: str, upload_id: str) -> Optional[Tuple[str, str, str, str]]:
        """``(path, media type, name, kind)`` of an upload of THIS session whose file still exists."""
        with self._lock:
            rec = self._items.get(upload_id)
            if rec is None or rec["session_key"] != session_key or not os.path.isfile(rec["path"]):
                return None
            return rec["path"], rec["type"], rec["name"], rec["kind"]

    def attachments(self, session_key: str, client_message_id: Any) -> List[Dict[str, Any]]:
        if not isinstance(client_message_id, str) or not client_message_id:
            return []
        with self._lock:
            ids = self._bound.get((session_key, client_message_id)) or []
            return [self.public(self._items[i]) for i in ids if i in self._items]

    # -- bind to a message -----------------------------------------------------------------------
    def bind(self, session_key: str, client_message_id: str, ids: Any) -> List[Dict[str, Any]]:
        """Bind *ids* (this session's, unbound, existing, 1..``MAX_PER_MESSAGE``, no repeats) to the
        message; returns the full records in the given order. ``UploadError(400)`` otherwise."""
        if (not isinstance(ids, list) or not 0 < len(ids) <= MAX_PER_MESSAGE
                or not all(isinstance(i, str) and ID_RE.fullmatch(i) for i in ids) or len(set(ids)) != len(ids)):
            raise UploadError(400, "bad attachments",
                              f"Anhänge: 1 bis {MAX_PER_MESSAGE} gültige Upload-IDs ohne Wiederholung.")
        self.sweep()
        with self._lock:
            recs = [self._items.get(i) for i in ids]
            if any(r is None or r["session_key"] != session_key or r["bound"] or not os.path.isfile(r["path"])
                   for r in recs):
                raise UploadError(400, "unknown attachment",
                                  "Ein Anhang ist unbekannt, abgelaufen oder schon gesendet.")
            for r in recs:
                r["bound"] = client_message_id
            self._bound[(session_key, client_message_id)] = list(ids)
            return [dict(r) for r in recs]

    def release(self, session_key: str, client_message_id: str) -> None:
        """The message was not accepted: its uploads are unbound again (the board may resend them)."""
        with self._lock:
            for upload_id in self._bound.pop((session_key, client_message_id), []):
                rec = self._items.get(upload_id)
                if rec is not None:
                    rec["bound"] = None
                    rec["created"] = self.clock()

"""WB-1321: files Hermes SENDS in a turn, for the Werkbank chat — the way Telegram gets them.

Why this exists: the ``werkbank`` plugin has no messaging adapter (``__init__.py``). The gateway
delivers a reply's ``MEDIA:<path>`` files only through the session origin's adapter
(``gateway/platforms/base.py`` ``_deliver_media_attachments`` → ``send_voice`` / ``send_video`` /
``send_document`` / the image batch; streamed replies ``gateway/run_notifications.py``
``_deliver_media_from_response``). The board only sees session events, and ``turn.done`` carries the
reply text with the raw ``MEDIA:`` tags (``gateway/session_events.end_turn``; auto-appended tags of
TTS and co. from ``run_turn_runner._append_auto_media_tags``). Nothing ever handed the files to the
board — so it showed none.

What it does: on ``turn.done`` / ``turn.stopped`` of a session the board watches, the tags are read
with the gateway's OWN extractor and delivery filter (``BasePlatformAdapter.extract_media`` +
``filter_media_delivery_paths`` — the same files Telegram gets, the same denylist), each file is
COPIED into an outbox of this plugin (never a reference to the original path), the event gets
``payload.files`` (metadata only) and the tags leave the text. The Telegram delivery is untouched:
the core's event dict is never changed, the plugin works on a copy.

Storage: ``<home>/plugin-data/werkbank/outbox/<session digest>/<file id>/`` (directories 0700,
files 0600, ``O_EXCL``); ``meta.json`` next to the bytes, so a gateway restart keeps the chat's
files. File ids are 128 random bits (``fx_<32 hex>``). At most ``MAX_FILE`` (50 MB — the Telegram
bot upload limit) per file; a bigger one becomes a card without bytes. Old files go after
``KEEP_S`` and when all files together pass ``MAX_TOTAL`` (oldest first).

Playback versions (``medien.Werkstatt``, background, one job at a time): images get a small
(≤ 360 px) and a large (≤ 2048 px) WebP preview, videos a JPEG poster and a faststart H.264/AAC MP4
(remuxed when the original already fits), audio iOS cannot play (Ogg/Opus, WebM) an AAC ``.m4a``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import uploads as uploads_mod

logger = logging.getLogger("plugins.platforms.werkbank")

MAX_FILE = 50 * 1024 * 1024          # = Telegram's bot upload limit
MAX_PER_TURN = 10
KEEP_S = 30 * 24 * 3600              # outbox files older than this are deleted
MAX_TOTAL = 4 * 1024 * 1024 * 1024   # all outbox files of this profile together (oldest go first)
SNAPSHOT_INGEST_ROWS = 10            # newest answer rows whose files a snapshot may still take over
ID_RE = re.compile(r"fx_[0-9a-f]{32}")
CHUNK = 256 * 1024

BILD, AUDIO, VIDEO, DATEI = "bild", "audio", "video", "datei"
WARTET, BEREIT, FEHLER = "wartet", "bereit", "fehler"

# variant -> (file name in the file's folder, media type); "original" is the copied file itself
VARIANTS = {"klein": ("klein.webp", "image/webp"), "gross": ("gross.webp", "image/webp"),
            "poster": ("poster.jpg", "image/jpeg"), "mp4": ("play.mp4", "video/mp4"),
            "m4a": ("play.m4a", "audio/mp4")}
KLEIN_PX, GROSS_PX, POSTER_PX = 360, 2048, 480
VIDEO_KURZSEITE = 1080

_FTYP_VIDEO = (b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"dash", b"M4V ",
               b"M4VP", b"mmp4", b"MSNV", b"3gp4", b"3gp5", b"3g2a")
_FTYP_AUDIO = (b"M4A ", b"M4B ", b"F4A ")
_HTML_EXT = (".html", ".htm", ".xhtml", ".shtml", ".mht", ".mhtml")
_TEXT_EXT = {".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv", ".log": "text/plain",
             ".json": "application/json", ".yaml": "application/yaml", ".yml": "application/yaml"}
_DOC_EXT = {".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".zip": "application/zip"}


def _ext(name: str) -> str:
    ext = os.path.splitext(name or "")[1].lower()
    return ext if re.fullmatch(r"\.[a-z0-9]{1,8}", ext) else ""


def sniff(head: bytes, name: str) -> Tuple[str, str, bool]:
    """``(kind, media type, download only)`` of a file from its first bytes and name. HTML and SVG
    (script in the board's origin) and anything unknown are download only."""
    ext = _ext(name)
    lowered = head[:512].lstrip().lower()
    if ext in _HTML_EXT or lowered.startswith((b"<!doctype html", b"<html")):
        return DATEI, "text/html", True
    if ext == ".svg" or lowered.startswith(b"<svg") or (lowered.startswith(b"<?xml") and b"<svg" in lowered):
        return DATEI, "image/svg+xml", True
    if head.startswith(b"\xff\xd8\xff"):
        return BILD, "image/jpeg", False
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return BILD, "image/png", False
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return BILD, "image/gif", False
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return BILD, "image/webp", False
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return AUDIO, "audio/wav", False
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return VIDEO, "video/x-msvideo", False
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _FTYP_AUDIO:
            return AUDIO, "audio/mp4", False
        if brand == b"qt  ":
            return VIDEO, "video/quicktime", False
        if brand in _FTYP_VIDEO:
            return VIDEO, "video/mp4", False
        return DATEI, "application/octet-stream", True          # HEIC and co.: a file
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return VIDEO, ("video/x-matroska" if ext == ".mkv" else "video/webm"), False
    if head.startswith(b"OggS"):
        return AUDIO, "audio/ogg", False
    if head.startswith(b"fLaC"):
        return AUDIO, "audio/flac", False
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return AUDIO, "audio/mpeg", False
    if head[:2] in (b"\xff\xf1", b"\xff\xf9"):
        return AUDIO, "audio/aac", False
    if head.startswith(b"%PDF-"):
        return DATEI, "application/pdf", False
    if ext in _TEXT_EXT and b"\x00" not in head:
        return DATEI, _TEXT_EXT[ext], False
    if head.startswith(b"PK\x03\x04") and ext in _DOC_EXT:
        return DATEI, _DOC_EXT[ext], True
    return DATEI, "application/octet-stream", True


def extract(text: str, session_key: str) -> Tuple[List[Tuple[str, bool]], str]:
    """The reply's deliverable ``MEDIA:`` files and the text without the tags — with the gateway's
    own extractor and delivery filter (what Telegram gets). ``([], text)`` when the core lacks them."""
    if "MEDIA:" not in (text or ""):
        return [], text
    try:
        from gateway.platforms.base import BasePlatformAdapter
        media, cleaned = BasePlatformAdapter.extract_media(text)
        media = BasePlatformAdapter.filter_media_delivery_paths(media, session_key)
    except Exception:
        logger.info("werkbank: MEDIA tags not readable (gateway extractor missing)")
        return [], text
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return list(media), cleaned


def _secure_dir(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(folder, 0o700)


def _digest(session_key: str) -> str:
    return hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:24]


class Outbox:
    """Thread-safe: the hook thread ingests, HTTP threads read, the workshop thread finishes."""

    def __init__(self, home: Path, *, werkstatt: Any = None, clock: Callable[[], float] = time.time,
                 max_file: int = MAX_FILE, max_total: int = MAX_TOTAL, keep_s: float = KEEP_S) -> None:
        self.root = Path(home) / "plugin-data" / "werkbank" / "outbox"
        self.werkstatt = werkstatt
        self.clock = clock
        self.max_file, self.max_total, self.keep_s = max_file, max_total, keep_s
        self._lock = threading.RLock()
        self._loaded: set = set()                       # session digests read from disk
        self._items: Dict[str, Dict[str, Any]] = {}     # file id -> meta

    # -- index ------------------------------------------------------------------------------------
    def _folder(self, digest: str, fid: str) -> Path:
        return self.root / digest / fid

    def _load(self, digest: str) -> None:
        if digest in self._loaded:
            return
        self._loaded.add(digest)
        base = self.root / digest
        try:
            entries = list(base.iterdir())
        except OSError:
            return
        for entry in entries:
            if not ID_RE.fullmatch(entry.name):
                continue
            try:
                meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(meta, dict) and meta.get("id") == entry.name and meta.get("digest") == digest:
                for name, state in list((meta.get("fassungen") or {}).items()):
                    if state == WARTET:            # the job died with the old process
                        meta["fassungen"][name] = FEHLER
                self._items[entry.name] = meta

    def _save(self, meta: Dict[str, Any]) -> None:
        folder = self._folder(meta["digest"], meta["id"])
        part = folder / "meta.json.part"
        data = json.dumps(meta, ensure_ascii=False).encode("utf-8")
        fd = os.open(str(part), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(part, folder / "meta.json")

    @staticmethod
    def public(meta: Dict[str, Any]) -> Dict[str, Any]:
        out = {k: meta[k] for k in ("id", "name", "type", "size", "kind", "nur_download", "abspielbar")
               if k in meta}
        out["fassungen"] = dict(meta.get("fassungen") or {})
        for k in ("voice", "dauer", "breite", "hoehe", "grund", "fehlt", "grenze"):
            if meta.get(k) not in (None, ""):
                out[k] = meta[k]
        return out

    def get(self, session_key: str, fid: str) -> Optional[Dict[str, Any]]:
        if not isinstance(fid, str) or not ID_RE.fullmatch(fid):
            return None
        digest = _digest(session_key)
        with self._lock:
            self._load(digest)
            meta = self._items.get(fid)
            return dict(meta) if meta and meta.get("digest") == digest else None

    def path(self, session_key: str, fid: str, variant: str) -> Optional[Tuple[Path, str]]:
        """``(path, media type)`` of an existing variant of a file of THIS session."""
        meta = self.get(session_key, fid)
        if meta is None:
            return None
        folder = self._folder(meta["digest"], fid)
        if variant == "original":
            found = folder / "original"
            media_type = meta["type"]
        elif variant in VARIANTS and (meta.get("fassungen") or {}).get(variant) == BEREIT:
            found = folder / VARIANTS[variant][0]
            media_type = VARIANTS[variant][1]
        else:
            return None
        return (found, media_type) if found.is_file() and not found.is_symlink() else None

    def for_refs(self, session_key: str, refs: Iterable[Any]) -> List[Dict[str, Any]]:
        wanted = {str(r) for r in refs if isinstance(r, (str, int)) and not isinstance(r, bool) and str(r)}
        if not wanted:
            return []
        digest = _digest(session_key)
        with self._lock:
            self._load(digest)
            found = [m for m in self._items.values()
                     if m.get("digest") == digest and wanted & set(m.get("refs") or [])]
        found.sort(key=lambda m: (m.get("created") or 0, m.get("order") or 0))
        return [self.public(m) for m in found]

    def _add_refs(self, meta: Dict[str, Any], refs: List[str]) -> None:
        have = list(meta.get("refs") or [])
        new = [r for r in refs if r not in have]
        if new:
            meta["refs"] = have + new
            self._save(meta)

    # -- ingest -----------------------------------------------------------------------------------
    def ingest(self, session_key: str, media: List[Tuple[str, bool]], refs: Iterable[Any]) -> List[Dict[str, Any]]:
        """Copy the reply's files into the outbox (each once per session: the same source file — path,
        size, mtime — is one entry, its refs grow) and queue their versions. Returns public entries."""
        refs = [str(r) for r in refs if isinstance(r, (str, int)) and not isinstance(r, bool) and str(r)]
        digest = _digest(session_key)
        out: List[Dict[str, Any]] = []
        for order, (src, is_voice) in enumerate(media[:MAX_PER_TURN]):
            entry = self._ingest_one(session_key, digest, str(src), bool(is_voice), refs, order)
            if entry is not None:
                out.append(entry)
        self.prune()
        return out

    def _ingest_one(self, session_key, digest, src, is_voice, refs, order) -> Optional[Dict[str, Any]]:
        name = os.path.basename(src)
        try:
            fd = os.open(src, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            # the gateway's filter resolved symlinks already; a vanished file is a card without bytes
            return {"id": None, "name": uploads_mod.clean_name(name, _ext(name)), "size": 0, "kind": DATEI,
                    "fassungen": {}, "fehlt": "nicht_lesbar"}
        with os.fdopen(fd, "rb") as source:
            st = os.fstat(source.fileno())
            if not stat.S_ISREG(st.st_mode):
                return None
            src_key = hashlib.sha256(f"{os.path.realpath(src)}\0{st.st_size}\0{st.st_mtime_ns}".encode()).hexdigest()
            with self._lock:
                self._load(digest)
                same = next((m for m in self._items.values()
                             if m.get("digest") == digest and m.get("src") == src_key), None)
                if same is not None:
                    self._add_refs(same, refs)
                    return self.public(same)
            head = source.read(4096)
            kind, media_type, download_only = sniff(head, name)
            shown = uploads_mod.clean_name(name, _ext(name))
            if st.st_size > self.max_file:
                return {"id": None, "name": shown, "size": st.st_size, "kind": kind, "type": media_type,
                        "fassungen": {}, "fehlt": "zu_gross", "grenze": self.max_file}
            fid = "fx_" + secrets.token_hex(16)
            folder = self._folder(digest, fid)
            try:
                _secure_dir(self.root)
                _secure_dir(self.root / digest)
                folder.mkdir(mode=0o700)
                size = self._copy(source, head, folder / "original")
            except (OSError, ValueError) as exc:
                logger.info("werkbank: outbox copy failed (%s)", type(exc).__name__)
                return {"id": None, "name": shown, "size": st.st_size, "kind": kind, "type": media_type,
                        "fassungen": {}, "fehlt": "zu_gross" if isinstance(exc, ValueError) else "nicht_lesbar",
                        "grenze": self.max_file}
        meta = {"id": fid, "digest": digest, "src": src_key, "name": shown, "type": media_type, "size": size,
                "kind": kind, "nur_download": download_only, "voice": bool(is_voice and kind == AUDIO) or None,
                "abspielbar": kind in (BILD, AUDIO) or (kind == VIDEO and media_type == "video/mp4"),
                "created": self.clock(), "order": order, "refs": list(refs), "fassungen": {}}
        self._plan(meta)
        with self._lock:
            self._items[fid] = meta
            self._save(meta)
        self._queue(meta)
        return self.public(meta)

    def _copy(self, source, head: bytes, target: Path) -> int:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(target), flags, 0o600)
        size = 0
        try:
            with os.fdopen(fd, "wb") as out:
                chunk = head
                while chunk:
                    size += len(chunk)
                    if size > self.max_file:
                        raise ValueError("grew past the limit")
                    out.write(chunk)
                    chunk = source.read(CHUNK)
        except BaseException:
            try:
                target.unlink()
            except OSError:
                pass
            raise
        return size

    # -- versions ---------------------------------------------------------------------------------
    def _plan(self, meta: Dict[str, Any]) -> None:
        w = self.werkstatt
        if meta["kind"] not in (BILD, VIDEO, AUDIO):
            return
        if w is None or not getattr(w, "available", False):
            meta["grund"] = "ffmpeg fehlt auf dem Hermes-Rechner: nur das Original"
            return
        f = meta["fassungen"]
        if meta["kind"] == BILD:
            f["klein"] = WARTET
            if meta["type"] != "image/gif":          # an animated GIF keeps its frames: the original
                f["gross"] = WARTET
        elif meta["kind"] == VIDEO:
            f["poster"] = WARTET
            f["mp4"] = WARTET
        elif meta["kind"] == AUDIO and meta["type"] in ("audio/ogg", "audio/webm"):
            f["m4a"] = WARTET

    def _queue(self, meta: Dict[str, Any]) -> None:
        w = self.werkstatt
        if w is None or not meta["fassungen"]:
            return
        fid, digest = meta["id"], meta["digest"]
        if meta["kind"] == BILD:
            w.submit(lambda: self._job_image(digest, fid), prio=0)
        elif meta["kind"] == VIDEO:
            w.submit(lambda: self._job_video_head(digest, fid), prio=0)
            w.submit(lambda: self._job_video_mp4(digest, fid), prio=1)
        elif meta["kind"] == AUDIO:
            w.submit(lambda: self._job_audio(digest, fid), prio=1)

    def _meta(self, digest: str, fid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            meta = self._items.get(fid)
            return meta if meta and meta.get("digest") == digest else None

    def _done(self, digest: str, fid: str, variant: str, ok: bool, **info) -> None:
        with self._lock:
            meta = self._meta(digest, fid)
            if meta is None:
                return
            meta["fassungen"][variant] = BEREIT if ok else FEHLER
            meta.update({k: v for k, v in info.items() if v is not None})
            try:
                self._save(meta)
            except OSError:
                pass

    @staticmethod
    def _longest(px: int) -> str:
        return f"scale='if(gte(iw,ih),min({px},iw),-2)':'if(gte(iw,ih),-2,min({px},ih))'"

    def _job_image(self, digest: str, fid: str) -> None:
        folder = self._folder(digest, fid)
        src = str(folder / "original")
        w = self.werkstatt
        ok = w.ffmpeg(["-i", src, "-frames:v", "1", "-vf", self._longest(KLEIN_PX), "-c:v", "libwebp",
                       "-quality", "55"], folder / VARIANTS["klein"][0], fmt="webp", timeout=60)
        self._done(digest, fid, "klein", ok)
        meta = self._meta(digest, fid)
        if meta and "gross" in meta["fassungen"]:
            ok = w.ffmpeg(["-i", src, "-frames:v", "1", "-vf", self._longest(GROSS_PX), "-c:v", "libwebp",
                           "-quality", "80"], folder / VARIANTS["gross"][0], fmt="webp", timeout=120)
            self._done(digest, fid, "gross", ok)

    @staticmethod
    def _streams(probe: Optional[Dict[str, Any]], kind: str) -> List[Dict[str, Any]]:
        return [s for s in (probe or {}).get("streams") or [] if isinstance(s, dict) and s.get("codec_type") == kind]

    def _job_video_head(self, digest: str, fid: str) -> None:
        """Probe (duration, size, does the original play as it is) and the poster."""
        folder = self._folder(digest, fid)
        src = folder / "original"
        probe = self.werkstatt.probe(src)
        video = (self._streams(probe, "video") or [{}])[0]
        audio = (self._streams(probe, "audio") or [None])[0]
        try:
            dauer = round(float((probe or {}).get("format", {}).get("duration")), 1)
        except (TypeError, ValueError):
            dauer = None
        fmt = str((probe or {}).get("format", {}).get("format_name") or "")
        playable = (video.get("codec_name") == "h264" and video.get("pix_fmt") in ("yuv420p", "yuvj420p")
                    and ("mp4" in fmt or "mov" in fmt)
                    and (audio is None or audio.get("codec_name") in ("aac", "mp3")))
        with self._lock:
            meta = self._meta(digest, fid)
            if meta is not None:
                meta.update(dauer=dauer, breite=video.get("width"), hoehe=video.get("height"),
                            abspielbar=bool(playable), probe=self._probe_view(probe))
                self._save(meta)
        args = ["-i", str(src), "-frames:v", "1", "-vf", self._longest(POSTER_PX), "-c:v", "mjpeg", "-q:v", "7"]
        ok = self.werkstatt.ffmpeg(["-ss", "0.5"] + args, folder / VARIANTS["poster"][0], fmt="mjpeg", timeout=60)
        if not ok:                                   # shorter than half a second
            ok = self.werkstatt.ffmpeg(args, folder / VARIANTS["poster"][0], fmt="mjpeg", timeout=60)
        self._done(digest, fid, "poster", ok)

    @staticmethod
    def _probe_view(probe: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """What the encode decision needs, nothing more (the probe may name the file's tags)."""
        video = (Outbox._streams(probe, "video") or [{}])[0]
        audio = (Outbox._streams(probe, "audio") or [{}])[0]
        return {"format": str((probe or {}).get("format", {}).get("format_name") or ""),
                "v": {k: video.get(k) for k in ("codec_name", "profile", "level", "pix_fmt", "width", "height")},
                "a": audio.get("codec_name")}

    @staticmethod
    def remux_enough(view: Dict[str, Any]) -> bool:
        """The original is already H.264 (High/Main/Baseline, level ≤ 4.1, yuv420p, short side ≤ 1080)
        with AAC or no audio in MP4/MOV: copy the streams, only move the index to the front."""
        v = view.get("v") or {}
        try:
            small = min(int(v.get("width") or 0), int(v.get("height") or 0)) <= VIDEO_KURZSEITE
            level_ok = 0 < int(v.get("level") or 0) <= 41
        except (TypeError, ValueError):
            return False
        return ("mp4" in view.get("format", "") or "mov" in view.get("format", "")) and \
            v.get("codec_name") == "h264" and v.get("pix_fmt") == "yuv420p" and small and level_ok and \
            str(v.get("profile") or "") in ("High", "Main", "Baseline", "Constrained Baseline") and \
            view.get("a") in (None, "aac")

    def _job_video_mp4(self, digest: str, fid: str) -> None:
        folder = self._folder(digest, fid)
        meta = self._meta(digest, fid) or {}
        view = meta.get("probe") or {}
        out = folder / VARIANTS["mp4"][0]
        maps = ["-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]
        if self.remux_enough(view):
            ok = self.werkstatt.ffmpeg(["-i", str(folder / "original"), *maps, "-c", "copy",
                                        "-movflags", "+faststart"], out, fmt="mp4")
            how = "remux"
        else:
            scale = (f"scale='if(gte(iw,ih),-2,min({VIDEO_KURZSEITE},iw))':"
                     f"'if(gte(iw,ih),min({VIDEO_KURZSEITE},ih),-2)',scale=trunc(iw/2)*2:trunc(ih/2)*2")
            ok = self.werkstatt.ffmpeg(
                ["-i", str(folder / "original"), *maps, "-vf", scale,
                 "-c:v", "libx264", "-profile:v", "high", "-level:v", "4.1", "-pix_fmt", "yuv420p",
                 "-preset", "veryfast", "-crf", "26", "-c:a", "aac", "-b:a", "128k", "-ac", "2",
                 "-movflags", "+faststart"], out, fmt="mp4")
            how = "kodiert"
        self._done(digest, fid, "mp4", ok, mp4_weg=how if ok else None)

    def _job_audio(self, digest: str, fid: str) -> None:
        folder = self._folder(digest, fid)
        probe = self.werkstatt.probe(folder / "original")
        try:
            dauer = round(float((probe or {}).get("format", {}).get("duration")), 1)
        except (TypeError, ValueError):
            dauer = None
        ok = self.werkstatt.ffmpeg(["-i", str(folder / "original"), "-vn", "-c:a", "aac", "-b:a", "128k",
                                    "-movflags", "+faststart"], folder / VARIANTS["m4a"][0], fmt="mp4")
        self._done(digest, fid, "m4a", ok, dauer=dauer)

    # -- housekeeping -----------------------------------------------------------------------------
    def prune(self) -> int:
        """Delete files older than ``keep_s``, then the oldest until all together fit ``max_total``.
        Returns how many file folders went."""
        removed = 0
        now = self.clock()
        try:
            digests = [d.name for d in self.root.iterdir() if re.fullmatch(r"[0-9a-f]{24}", d.name)]
        except OSError:
            return 0
        with self._lock:
            for digest in digests:
                self._load(digest)
            items = sorted(self._items.values(), key=lambda m: m.get("created") or 0)
            sizes = {m["id"]: self._folder_size(self._folder(m["digest"], m["id"])) for m in items}
            total = sum(sizes.values())
            for meta in items:
                old = now - (meta.get("created") or 0) > self.keep_s
                if not old and total <= self.max_total:
                    continue
                if WARTET in (meta.get("fassungen") or {}).values() and not old:
                    continue                          # a job still writes into it
                self._remove(meta)
                total -= sizes.get(meta["id"], 0)
                removed += 1
        return removed

    @staticmethod
    def _folder_size(folder: Path) -> int:
        total = 0
        try:
            for entry in folder.iterdir():
                try:
                    total += entry.lstat().st_size
                except OSError:
                    pass
        except OSError:
            pass
        return total

    def _remove(self, meta: Dict[str, Any]) -> None:
        folder = self._folder(meta["digest"], meta["id"])
        self._items.pop(meta["id"], None)
        try:
            for entry in folder.iterdir():
                if entry.is_file() or entry.is_symlink():
                    entry.unlink()
            folder.rmdir()
        except OSError:
            pass

    # -- the two places the board sees files ------------------------------------------------------
    def annotate(self, event: Any, watched: Callable[[str], Any]) -> Any:
        """``turn.done`` / ``turn.stopped`` of a watched session with ``MEDIA:`` tags: a COPY of the
        event with ``payload.files`` and the text without tags. Anything else: *event* itself."""
        if not isinstance(event, dict) or event.get("type") not in ("turn.done", "turn.stopped"):
            return event
        key = event.get("session_key")
        payload = event.get("payload")
        if not isinstance(key, str) or not isinstance(payload, dict) or watched(key) is None:
            return event
        text = payload.get("final_response")
        if not isinstance(text, str) or "MEDIA:" not in text:
            return event
        media, cleaned = extract(text, key)
        refs = [r for r in (payload.get("message_uid"), event.get("id")) if r]
        try:
            files = self.ingest(key, media, refs) if media else []
        except Exception:
            logger.info("werkbank: outbox ingest failed (%s)", "internal")
            files = []
        new_payload = {**payload, "final_response": cleaned}
        if files:
            new_payload["files"] = files
        return {**event, "payload": new_payload}

    def dress(self, session_key: str, messages: List[Dict[str, Any]]) -> None:
        """Snapshot: every assistant message loses its ``MEDIA:`` tags and gets its ``files`` — those
        the outbox holds for its answer row (``message_uid``) or turn, else (the newest
        ``SNAPSHOT_INGEST_ROWS`` answers only) the files its tags name, taken over now."""
        answers = [m for m in messages if isinstance(m, dict) and m.get("role") == "assistant"]
        recent = {id(m) for m in [m for m in answers if "MEDIA:" in str(m.get("text") or "")][-SNAPSHOT_INGEST_ROWS:]}
        for m in answers:
            refs = [r for r in (m.get("message_uid"), m.get("id")) if r]
            files = self.for_refs(session_key, refs)
            text = m.get("text")
            if isinstance(text, str) and "MEDIA:" in text:
                media, cleaned = extract(text, session_key)
                m["text"] = cleaned
                if media and not files and id(m) in recent:
                    try:
                        files = self.ingest(session_key, media, refs[:1])
                    except Exception:
                        files = []
            if files:
                m["files"] = files


def parse_range(header: Optional[str], size: int):
    """``None`` (whole file), ``(start, end)`` inclusive, or ``"416"`` (unsatisfiable). A malformed or
    multi-range header is ignored (RFC 9110: the whole file)."""
    if not header:
        return None
    m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", header)
    if not m or (not m.group(1) and not m.group(2)):
        return None
    first, last = m.group(1), m.group(2)
    if first:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
        if last and int(last) < start:
            return None
        if start >= size:
            return "416"
        return start, end
    suffix = int(last)
    if suffix == 0 or size == 0:
        return "416"
    return max(0, size - suffix), size - 1

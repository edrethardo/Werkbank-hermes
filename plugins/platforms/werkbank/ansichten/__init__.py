"""WB-1471: ``ansicht_zeigen`` — interactive views in the Werkbank chat (Chat-Ansichten).

The agent calls the tool with ``{art, titel?, daten | quelle, optionen?}``. This package checks the
call BEFORE anything is shown (schema per ``art``, limits, references) and answers a precise German
message on every error (field, expected, found), so the agent corrects itself. A valid view is

* stored as a file of this plugin (``<home>/plugin-data/werkbank/ansichten/<digest>/<id>.json``,
  0600) — the transcript keeps only the tool call and its answer ``{ok, id}``, the snapshot finds the
  view by that id after a reload, at the place of the tool call in the answer;
* sent as the session event ``ansicht`` (``gateway.session_events.emit``: the plugin buffer and every
  stream get it in core order, between ``tool.start`` and ``tool.end`` of the call).

BLOCKS ARE FILES. Every module ``<art>.py`` of this package (lower-case name, no underscore) is a
block and found on its own (:func:`bausteine`) — a new block never changes a shared file. The
contract is in ``README.md`` next to this file: ``ART``, ``KURZ``, ``IN_GRUPPE``,
``pruefen(args, feld, k)``.

No HTML, no style, no script from the agent: every text is plain data, links only http/https,
images and data files only by reference. A reference resolves ONLY to

* a file of this chat: ``fx_…`` (a file Hermes sent, outbox) or ``up_…`` (an attachment the owner
  sent) — of THIS session;
* an artefact of the board: ``/api/project-outbox/<projekt>/<name>`` (full URL allowed) — the board
  serves it with its owner check and answers 404 for a name it does not have;
* a file in the session's project folder (the board names the folder on ``/v1/resolve``): a relative
  path, every component opened with ``O_NOFOLLOW`` from the folder down (no ``..``, no symlink on the
  way, a regular file). It is COPIED into the outbox, the browser fetches the copy by its ``fx_`` id.

The tool exists only inside a gateway with this plugin on, and only for sessions the board attached
(``verfuegbar``: the session key was resolved by the board — remembered on disk across restarts).
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import pkgutil
import re
import secrets
import stat
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("plugins.platforms.werkbank")

TOOL_NAME = "ansicht_zeigen"
TOOLSET = "werkbank"
EVENT = "ansicht"
FELDER = ("art", "titel", "daten", "quelle", "optionen")   # the call's fields, for every block

MAX_INLINE = 200 * 1024                     # bytes of the whole call as JSON
MAX_TITEL = 200
MAX_URL = 2000
MAX_QUELLE = 2 * 1024 * 1024                # bytes of a data file the plugin reads to check it
MAX_FEHLER = 5                              # messages named in one answer
MAX_GESPEICHERT = 500                       # views kept per session (oldest go first)
MAX_SITZUNGEN = 256                         # attached sessions remembered
MAX_OFFEN = 20                              # views a snapshot places from the store before their row
MAX_GESPEICHERT_BYTES = 512 * 1024          # bytes of ONE checked view as stored and sent (JSON)
SNAPSHOT_BUDGET = 4 * 1024 * 1024           # bytes of views in one snapshot; older ones as a stub

ID_RE = re.compile(r"av_[0-9a-f]{24}")
ART_RE = re.compile(r"[a-z][a-z0-9]{1,30}")
OUTBOX_RE = re.compile(r"fx_[0-9a-f]{32}")
UPLOAD_RE = re.compile(r"up_[0-9a-f]{24}")
ARTEFAKT_RE = re.compile(r"(?:https?://[A-Za-z0-9.:\[\]-]+)?(/api/project-outbox/([A-Za-z0-9_-][A-Za-z0-9_.-]{0,127})/"
                         r"([A-Za-z0-9_-][A-Za-z0-9_.-]{0,199}))")
BILD_ENDUNGEN = (".png", ".jpg", ".jpeg", ".gif", ".webp")
DATEN_ENDUNGEN = {".csv": "csv", ".json": "json"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

BESCHREIBUNG_ANFANG = ("Zeigt eine interaktive Ansicht in deiner Antwort im Werkbank-Chat (Handy und PC); "
                       "die Bedienung bleibt im Browser.")
BESCHREIBUNG_ENDE = ("Verweise (quelle, Bilder): Chat-Datei fx_.../up_..., Artefakt /api/project-outbox/... "
                     "oder Pfad im Projektordner. Höchstens 200 KB. Antwort {ok,id}; ein Fehler nennt Feld, "
                     "erwartet und gefunden.")


class Fehler(Exception):
    """One precise message: ``<feld>: erwartet <x>, gefunden <y>`` or a sentence about the field."""


def _digest(session_key: str) -> str:
    return hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:24]


# -- helpers for blocks (public: a block module imports them) ---------------------------------------
def was(value: Any) -> str:
    """What the agent sent, in words (with a short quote of a text)."""
    if value is None:
        return "nichts"
    if isinstance(value, bool):
        return "Wahrheitswert"
    if isinstance(value, (int, float)):
        return f"Zahl {value!r}"
    if isinstance(value, str):
        shown = value if len(value) <= 40 else value[:37] + "…"
        return f"Text „{shown}“"
    if isinstance(value, list):
        return f"Liste mit {len(value)} Einträgen"
    if isinstance(value, dict):
        return "Objekt"
    return type(value).__name__


def erwartet(feld: str, soll: str, value: Any) -> Fehler:
    return Fehler(f"{feld}: erwartet {soll}, gefunden {was(value)}")


def nur_felder(obj: Dict[str, Any], feld: str, erlaubt: Tuple[str, ...]) -> None:
    fremd = [k for k in obj if k not in erlaubt]
    if fremd:
        raise Fehler(f"{feld}{'.' if feld else ''}{fremd[0]}: unbekanntes Feld, erlaubt: {', '.join(erlaubt)}")


def titel(value: Any, feld: str, *, pflicht: bool = False) -> str:
    if value is None and not pflicht:
        return ""
    if not isinstance(value, str) or (pflicht and not value.strip()):
        raise erwartet(feld, f"Text (höchstens {MAX_TITEL} Zeichen)", value)
    if len(value) > MAX_TITEL:
        raise Fehler(f"{feld}: höchstens {MAX_TITEL} Zeichen, gefunden {len(value)}")
    return _CONTROL.sub(" ", value).strip()


def link(value: Any, feld: str) -> str:
    """A link the browser may open: http(s) only, no spaces or control characters."""
    if not isinstance(value, str) or not re.match(r"(?i)https?://[^\s/]", value) or _CONTROL.search(value) \
            or len(value) > MAX_URL or any(c.isspace() for c in value):
        raise erwartet(feld, "Link mit http:// oder https://", value)
    return value


def iso_datum(text: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:?\d{2})?)?", text):
        return False
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return False
    return True


# -- references ------------------------------------------------------------------------------------
class Aufloeser:
    """Resolves the references of ONE call for ONE session (outbox, uploads, project folder)."""

    MAX_BILDER = 50                         # distinct image references of one view
    MAX_BILD_BYTES = 10 * 1024 * 1024       # one image from the project folder (before the copy)
    MAX_BILDER_BYTES = 50 * 1024 * 1024     # images from the project folder of one view together

    def __init__(self, session_key: str, projekt: Optional[str], outbox: Any = None, uploads: Any = None) -> None:
        self.session_key = session_key
        self.projekt = projekt
        self.outbox = outbox
        self.uploads = uploads
        self.bilder: Dict[str, Dict[str, Any]] = {}
        self.bild_bytes = 0                 # project images of this view, copied so far

    def bild(self, value: Any, feld: str) -> Dict[str, Any]:
        """An image reference (one copy per distinct reference, at most ``MAX_BILDER``)."""
        key = value.strip() if isinstance(value, str) else value
        if isinstance(key, str) and key in self.bilder:
            return dict(self.bilder[key])
        if len(self.bilder) >= self.MAX_BILDER:
            raise Fehler(f"{feld}: höchstens {self.MAX_BILDER} verschiedene Bilder je Ansicht")
        ref, _data = self.loese(value, feld, bild=True)
        self.bilder[key] = ref
        return dict(ref)

    def loese(self, value: Any, feld: str, *, bild: bool = False, daten: bool = False) -> Tuple[Dict[str, Any], Optional[bytes]]:
        """``(reference for the board, the bytes when the plugin could read them)``; raises Fehler."""
        if not isinstance(value, str) or not value.strip():
            raise erwartet(feld, "Verweis (fx_…, up_…, /api/project-outbox/… oder Pfad im Projektordner)", value)
        text = value.strip()
        if OUTBOX_RE.fullmatch(text):
            return self._outbox(text, feld, bild=bild, daten=daten)
        if UPLOAD_RE.fullmatch(text):
            return self._upload(text, feld, bild=bild, daten=daten)
        m = ARTEFAKT_RE.fullmatch(text)
        if m:
            name = m.group(3)
            if ".." in (m.group(2), name):
                raise Fehler(f"{feld}: ungültiger Artefakt-Pfad „{text[:200]}“")
            self._endung(name, feld, bild=bild, daten=daten)
            return {"artefakt": m.group(1), "name": name}, None
        if "://" in text or text.startswith("/api/"):
            raise Fehler(f"{feld}: „{text[:200]}“ ist kein erlaubter Verweis — nur Chat-Dateien (fx_…, up_…), "
                         "Artefakte (/api/project-outbox/…) oder Dateien im Projektordner")
        return self._projekt(text, feld, bild=bild, daten=daten)

    @staticmethod
    def _endung(name: str, feld: str, *, bild: bool, daten: bool) -> None:
        low = name.lower()
        if bild and not low.endswith(BILD_ENDUNGEN):
            raise Fehler(f"{feld}: „{name}“ ist kein Bild (erlaubt: {', '.join(BILD_ENDUNGEN)})")
        if daten and os.path.splitext(low)[1] not in DATEN_ENDUNGEN:
            raise Fehler(f"{feld}: „{name}“ ist keine CSV- oder JSON-Datei")

    def _outbox(self, fid: str, feld: str, *, bild: bool, daten: bool):
        meta = self.outbox.get(self.session_key, fid) if self.outbox is not None else None
        if not meta:
            raise Fehler(f"{feld}: {fid} ist keine Datei dieses Chats")
        if bild and meta.get("kind") != "bild":
            raise Fehler(f"{feld}: {fid} („{meta.get('name')}“) ist kein Bild")
        data = None
        if daten:
            self._endung(str(meta.get("name") or ""), feld, bild=False, daten=True)
            found = self.outbox.path(self.session_key, fid, "original")
            data = _read_capped(found[0], feld) if found else None
        return {"datei": fid, "name": meta.get("name"), "typ": meta.get("type")}, data

    def _upload(self, uid: str, feld: str, *, bild: bool, daten: bool):
        found = self.uploads.file(self.session_key, uid) if self.uploads is not None else None
        if not found:
            raise Fehler(f"{feld}: {uid} ist kein Anhang dieses Chats (oder schon gelöscht)")
        path, media_type, name, kind = found
        if bild and kind != "image":
            raise Fehler(f"{feld}: {uid} („{name}“) ist kein Bild")
        if daten:
            self._endung(str(name or ""), feld, bild=False, daten=True)
        data = _read_capped(path, feld) if daten else None
        return {"anhang": uid, "name": name, "typ": media_type}, data

    def _projekt(self, text: str, feld: str, *, bild: bool, daten: bool):
        if not self.projekt:
            raise Fehler(f"{feld}: „{text[:200]}“ — diese Sitzung hat keinen Projektordner; "
                         "nur Chat-Dateien (fx_…, up_…) oder Artefakte gehen")
        root = os.path.normpath(self.projekt)
        rel = text
        if os.path.isabs(text):
            norm = os.path.normpath(text)
            if norm != root and not norm.startswith(root.rstrip("/") + "/"):
                raise Fehler(f"{feld}: „{text[:200]}“ liegt nicht im Projektordner {root}")
            rel = os.path.relpath(norm, root)
        parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
        if not parts or any(p == ".." for p in parts):
            raise Fehler(f"{feld}: „{text[:200]}“ liegt nicht im Projektordner {root} (kein „..“)")
        self._endung(parts[-1], feld, bild=bild, daten=daten)
        if self.outbox is None:
            raise Fehler(f"{feld}: die Dateiablage des Chats ist nicht eingerichtet")
        fd = _open_below(root, parts, feld, text)
        try:
            st = os.fstat(fd)
            real = os.path.realpath(f"/proc/self/fd/{fd}") if os.path.isdir("/proc/self/fd") else None
            data = None
            if daten:
                if st.st_size > MAX_QUELLE:
                    raise Fehler(f"{feld}: „{parts[-1]}“ ist {st.st_size // 1024} KB groß, erlaubt "
                                 f"{MAX_QUELLE // 1024} KB")
                with os.fdopen(os.dup(fd), "rb") as handle:
                    data = handle.read(MAX_QUELLE + 1)
            if bild:                        # caps BEFORE the copy: the size of the open file
                if st.st_size > self.MAX_BILD_BYTES:
                    raise Fehler(f"{feld}: „{parts[-1]}“ ist {_menge(st.st_size)} groß, erlaubt je Bild "
                                 f"{_menge(self.MAX_BILD_BYTES)}")
                if self.bild_bytes + st.st_size > self.MAX_BILDER_BYTES:
                    raise Fehler(f"{feld}: Bilder aus dem Projektordner zusammen höchstens "
                                 f"{_menge(self.MAX_BILDER_BYTES)} je Ansicht")
            # the copy comes from THIS descriptor (no second open by path: a swap in between
            # cannot put another file into the chat)
            entry = self.outbox.ingest_fd(self.session_key, fd, parts[-1], ["ansicht"])
        finally:
            os.close(fd)
        if not entry or not entry.get("id"):
            raise Fehler(f"{feld}: „{parts[-1]}“ ließ sich nicht übernehmen "
                         f"({(entry or {}).get('fehlt') or 'Hermes liefert diese Datei nicht aus'})")
        # defence in depth: the copy's source key must be the checked file's; a wrong NEW copy
        # is taken back again
        meta = self.outbox.get(self.session_key, entry["id"]) or {}
        want = hashlib.sha256(f"{real}\0{st.st_size}\0{st.st_mtime_ns}".encode()).hexdigest() if real else None
        if want is None or meta.get("src") != want:
            if entry.get("neu"):
                self.outbox.entfernen(self.session_key, entry["id"])
            raise Fehler(f"{feld}: „{parts[-1]}“ hat sich während der Prüfung geändert; noch einmal aufrufen")
        if bild and entry.get("kind") != "bild":
            raise Fehler(f"{feld}: „{parts[-1]}“ ist kein Bild")
        if bild:
            self.bild_bytes += st.st_size
        return {"datei": entry["id"], "name": entry.get("name"), "typ": entry.get("type")}, data


def _menge(n: int) -> str:
    """A byte count in words (Bytes, KB, MB)."""
    if n < 1024:
        return f"{n} Bytes"
    if n < 1024 * 1024:
        return f"{n // 1024} KB"
    return f"{n // (1024 * 1024)} MB"


def _open_below(root: str, parts: List[str], feld: str, text: str) -> int:
    """A read descriptor of ``root/parts…`` that never followed a symlink below *root*."""
    flags_dir = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        current = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        raise Fehler(f"{feld}: der Projektordner {root} ist nicht lesbar") from None
    try:
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            try:
                info = os.stat(part, dir_fd=current, follow_symlinks=False)
            except FileNotFoundError:
                raise Fehler(f"{feld}: „{text[:200]}“ gibt es im Projektordner nicht") from None
            except OSError:
                raise Fehler(f"{feld}: „{text[:200]}“ ist nicht lesbar") from None
            if stat.S_ISLNK(info.st_mode):
                raise Fehler(f"{feld}: „{'/'.join(parts[:i + 1])}“ ist ein Symlink — Symlinks werden nicht verfolgt")
            if last:
                if not stat.S_ISREG(info.st_mode):
                    raise Fehler(f"{feld}: „{text[:200]}“ ist keine Datei")
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
            else:
                flags = flags_dir
            try:
                nxt = os.open(part, flags, dir_fd=current)
            except OSError:
                raise Fehler(f"{feld}: „{'/'.join(parts[:i + 1])}“ ist ein Symlink oder nicht lesbar") from None
            os.close(current)
            current = nxt
        if not stat.S_ISREG(os.fstat(current).st_mode):
            raise Fehler(f"{feld}: „{text[:200]}“ ist keine Datei")
        return current
    except BaseException:
        os.close(current)
        raise


def _read_capped(path: Any, feld: str) -> bytes:
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        raise Fehler(f"{feld}: die Datei ist nicht mehr lesbar") from None
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(MAX_QUELLE + 1)
    if len(data) > MAX_QUELLE:
        raise Fehler(f"{feld}: die Datei ist größer als {MAX_QUELLE // 1024} KB")
    return data


# -- blocks: one module per art, found on their own ------------------------------------------------
_BAUSTEINE: Optional[Dict[str, Any]] = None
_BAUSTEINE_LOCK = threading.Lock()


def bausteine() -> Dict[str, Any]:
    """``{art: module}`` of every block module in this package (``<art>.py`` with ``ART == <art>``
    and ``pruefen``), sorted by name. A broken module is left out (logged), never the whole tool."""
    global _BAUSTEINE
    with _BAUSTEINE_LOCK:
        if _BAUSTEINE is None:
            found: Dict[str, Any] = {}
            for info in pkgutil.iter_modules(__path__):        # type: ignore[name-defined]
                if info.ispkg or not ART_RE.fullmatch(info.name):
                    continue
                try:
                    mod = importlib.import_module(f"{__name__}.{info.name}")
                except Exception:
                    logger.warning("werkbank: chat view block %s not loadable", info.name, exc_info=True)
                    continue
                if getattr(mod, "ART", None) != info.name or not callable(getattr(mod, "pruefen", None)):
                    logger.warning("werkbank: chat view block %s breaks the contract (ART, pruefen)", info.name)
                    continue
                found[info.name] = mod
            _BAUSTEINE = dict(sorted(found.items()))
        return _BAUSTEINE


def arten(*, in_gruppe: bool = False) -> List[str]:
    return [a for a, mod in bausteine().items() if not in_gruppe or getattr(mod, "IN_GRUPPE", True)]


class Kontext:
    """What a block's ``pruefen`` gets besides its arguments: the resolver of this call (or None),
    the list for further messages (a block may collect several, e.g. one per cell) and
    :meth:`baustein` for nested blocks (a ``gruppe``)."""

    def __init__(self, aufl: Optional[Aufloeser], fehler: List[str]) -> None:
        self.aufl = aufl
        self.fehler = fehler

    def baustein(self, args: Any, feld: str) -> Dict[str, Any]:
        return _baustein(args, feld, self, in_gruppe=True)


def _baustein(args: Any, feld: str, k: Kontext, *, in_gruppe: bool) -> Dict[str, Any]:
    if not isinstance(args, dict):
        raise erwartet(feld.rstrip(".") or "Aufruf", "Objekt {art, titel?, daten | quelle, optionen?}", args)
    nur_felder(args, feld.rstrip("."), FELDER)
    art = args.get("art")
    mods = bausteine()
    if in_gruppe and art in mods and not getattr(mods[art], "IN_GRUPPE", True):
        raise Fehler(f"{feld}art: in einer gruppe stehen nur Bausteine ({', '.join(arten(in_gruppe=True))}), "
                     f"keine {art}")
    if art not in mods:
        raise erwartet(f"{feld}art", " oder ".join(arten(in_gruppe=in_gruppe)), art)
    out = {"art": art, "titel": titel(args.get("titel"), f"{feld}titel")}
    out.update(mods[art].pruefen(args, feld, k))
    out["art"] = art
    return out


def pruefen(args: Any, aufl: Optional[Aufloeser] = None) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """``(view, [])`` for a valid call, else ``(None, messages)`` — schema, limits, references."""
    try:
        size = len(json.dumps(args, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return None, ["Aufruf: erwartet JSON-Objekt"]
    if size > MAX_INLINE:
        return None, [f"Aufruf: {size // 1024} KB inline, erlaubt {MAX_INLINE // 1024} KB — große Tabellen "
                      "als quelle (CSV- oder JSON-Datei) übergeben"]
    fehler: List[str] = []
    try:
        view = _baustein(args, "", Kontext(aufl, fehler), in_gruppe=False)
    except Fehler as exc:
        return None, fehler + [str(exc)]
    if fehler:
        return None, fehler
    view = {"v": 1, **view}
    # the CHECKED view is what is stored and sent (references resolved, rows normalised)
    stored = len(json.dumps(view, ensure_ascii=False).encode("utf-8"))
    if stored > MAX_GESPEICHERT_BYTES:
        return None, [f"Ansicht: geprüft {_menge(stored)}, erlaubt {_menge(MAX_GESPEICHERT_BYTES)} — weniger "
                      "Zeilen oder Bilder, große Tabellen als quelle (CSV- oder JSON-Datei)"]
    return view, []


def fehlertext(fehler: List[str]) -> str:
    shown = fehler[:MAX_FEHLER]
    more = len(fehler) - len(shown)
    return ("Ansicht nicht gezeigt. " + "; ".join(shown)
            + (f" (und {more} weitere)" if more > 0 else "")
            + ". Korrigiere die Felder und rufe ansicht_zeigen erneut auf.")


def beschreibung() -> str:
    """The tool description: a fixed frame plus each block's ``KURZ`` line."""
    teile = [str(getattr(mod, "KURZ", "") or "").strip() for mod in bausteine().values()]
    return " ".join([BESCHREIBUNG_ANFANG, *[t for t in teile if t], BESCHREIBUNG_ENDE])


def schema() -> Dict[str, Any]:
    return {"name": TOOL_NAME, "description": beschreibung(),
            "parameters": {"type": "object",
                           "properties": {"art": {"type": "string", "enum": arten()},
                                          "titel": {"type": "string"}, "daten": {"type": "object"},
                                          "quelle": {"type": "string"}, "optionen": {"type": "object"}},
                           "required": ["art"]}}


# -- storage ---------------------------------------------------------------------------------------
class Ablage:
    """Views of the chat (one 0600 file each) and the sessions the board attached (with their project
    folder). Thread-safe; nothing here logs a text."""

    def __init__(self, home: Path, clock: Callable[[], float] = time.time) -> None:
        self.root = Path(home) / "plugin-data" / "werkbank" / "ansichten"
        self.clock = clock
        self._lock = threading.RLock()
        self._sitzungen: Optional[Dict[str, Dict[str, Any]]] = None
        self._offen: Dict[str, List[Dict[str, Any]]] = {}      # session digest -> views of this process

    # sessions --------------------------------------------------------------------------------
    def _sitzungen_laden(self) -> Dict[str, Dict[str, Any]]:
        if self._sitzungen is None:
            try:
                data = json.loads((self.root / "sitzungen.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            self._sitzungen = {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}
        return self._sitzungen

    def merke(self, session_key: str, projekt: Optional[str] = None) -> None:
        """The board attached *session_key* (``/v1/resolve``); *projekt* = its project folder."""
        if not isinstance(session_key, str) or not session_key:
            return
        if projekt is not None and (not isinstance(projekt, str) or not os.path.isabs(projekt)
                                    or not os.path.isdir(projekt)):
            projekt = None
        digest = _digest(session_key)
        with self._lock:
            known = self._sitzungen_laden()
            old = known.get(digest) or {}
            neu = {"projekt": projekt if projekt is not None else old.get("projekt"), "zeit": self.clock()}
            if old and old.get("projekt") == neu["projekt"] and self.clock() - (old.get("zeit") or 0) < 3600:
                return                                  # nothing new: no write on every resolve
            known.pop(digest, None)
            known[digest] = neu
            while len(known) > MAX_SITZUNGEN:
                known.pop(next(iter(known)))
            self._write(self.root / "sitzungen.json", known)

    def angehaengt(self, session_key: str) -> bool:
        if not isinstance(session_key, str) or not session_key:
            return False
        with self._lock:
            return _digest(session_key) in self._sitzungen_laden()

    def projekt(self, session_key: str) -> Optional[str]:
        with self._lock:
            entry = self._sitzungen_laden().get(_digest(session_key)) or {}
        found = entry.get("projekt")
        return found if isinstance(found, str) and os.path.isdir(found) else None

    # views -----------------------------------------------------------------------------------
    def _write(self, path: Path, data: Any) -> None:
        for folder in (self.root, path.parent):
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(folder, 0o700)
        part = path.with_name(path.name + ".part")
        fd = os.open(str(part), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        os.replace(part, path)

    def speichere(self, session_key: str, view: Dict[str, Any], *, tool_call_id: str = "",
                  session_id: Optional[str] = None, mark: Optional[int] = None) -> Dict[str, Any]:
        vid = "av_" + secrets.token_hex(12)
        entry = {"id": vid, "session": _digest(session_key), "tool_call_id": tool_call_id or None,
                 "session_id": session_id, "mark": mark, "zeit": self.clock(), "ansicht": {**view, "id": vid}}
        with self._lock:
            self._write(self.root / entry["session"] / f"{vid}.json", entry)
            offen = self._offen.setdefault(entry["session"], [])
            offen.append({k: entry[k] for k in ("id", "tool_call_id", "session_id", "mark")})
            del offen[:-MAX_OFFEN]
            self._prune(entry["session"])
        return entry

    def _prune(self, digest: str) -> None:
        try:
            files = sorted((p for p in (self.root / digest).iterdir()
                            if ID_RE.fullmatch(p.stem) and p.suffix == ".json"),
                           key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for old in files[:-MAX_GESPEICHERT]:
            try:
                old.unlink()
            except OSError:
                pass

    def lade(self, session_key: str, vid: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(vid, str) or not ID_RE.fullmatch(vid):
            return None
        path = self.root / _digest(session_key) / f"{vid}.json"
        try:
            fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as handle:
                entry = json.loads(handle.read().decode("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(entry, dict) or entry.get("id") != vid or entry.get("session") != _digest(session_key):
            return None
        return entry

    def offen(self, session_key: str) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._offen.get(_digest(session_key), [])]

    # the snapshot ----------------------------------------------------------------------------
    def einkleiden(self, session_key: str, messages: List[Dict[str, Any]], lineage: Optional[List[str]] = None) -> None:
        """Every ``ansicht_zeigen`` tool row of the history gets its view (``ansicht``), found by the id
        in the tool's answer; views of this process whose row is not written yet are placed by their
        row mark (``from_buffer``), so a reload during the turn still shows them."""
        have = set()
        for item in messages:
            if item.get("role") != "tool" or item.get("tool_name") != TOOL_NAME or item.get("is_error"):
                continue
            try:
                answer = json.loads(item.get("text") or "")
            except ValueError:
                continue
            vid = answer.get("id") if isinstance(answer, dict) and answer.get("ok") is True else None
            entry = self.lade(session_key, vid) if vid else None
            if entry:
                item["ansicht"] = entry["ansicht"]
                have.add(entry["id"])
                if entry.get("tool_call_id"):
                    have.add("call:" + str(entry["tool_call_id"]))
        allowed = set(lineage or [])
        rows = [m["db_id"] for m in messages if isinstance(m.get("db_id"), int)]
        low = min(rows) if rows else None
        for pending in self.offen(session_key):
            if pending["id"] in have or ("call:" + str(pending.get("tool_call_id"))) in have:
                continue
            if allowed and pending.get("session_id") not in allowed:
                continue
            mark = pending.get("mark")
            if low is not None and isinstance(mark, int) and 0 < mark < low:
                continue                                  # its row lies before the active history
            entry = self.lade(session_key, pending["id"])
            if not entry:
                continue
            at = len(messages)
            if isinstance(mark, int):
                for i, item in enumerate(messages):
                    rid = item.get("db_id")
                    if isinstance(rid, int) and rid > mark and not item.get("from_buffer"):
                        at = i
                        break
            messages.insert(at, {"id": entry["id"], "client_message_id": None, "message_id": None, "db_id": None,
                                 "role": "tool", "text": json.dumps({"ok": True, "id": entry["id"]}),
                                 "ts": None, "origin": None, "tool_call_id": entry.get("tool_call_id"),
                                 "tool_name": TOOL_NAME, "is_error": False, "truncated": False,
                                 "from_buffer": True, "ansicht": entry["ansicht"]})
        _budget(messages)


def _budget(messages: List[Dict[str, Any]]) -> None:
    """Newest views first up to ``SNAPSHOT_BUDGET`` bytes; every older one only as
    ``{id, art, titel, nachladen: true}`` — the board fetches it by id (``GET …/ansicht/{id}``)."""
    used = 0
    for item in reversed(messages):
        view = item.get("ansicht")
        if not isinstance(view, dict):
            continue
        size = len(json.dumps(view, ensure_ascii=False).encode("utf-8"))
        if used + size <= SNAPSHOT_BUDGET:
            used += size
            continue
        used = SNAPSHOT_BUDGET + 1                  # once over: every older view is a stub too
        item["ansicht"] = {"id": view.get("id"), "art": view.get("art"), "titel": view.get("titel") or "",
                           "nachladen": True}


# -- the tool --------------------------------------------------------------------------------------
def _current() -> Tuple[str, str]:
    """(session key, tool call id) of the running tool call."""
    key, call = "", ""
    try:
        from tools.approval_context import _approval_tool_call_id, get_current_session_key
        key = get_current_session_key("") or ""
        call = _approval_tool_call_id.get() or ""
    except Exception:
        try:
            from gateway.session_context import get_session_env
            key = get_session_env("HERMES_SESSION_KEY", "") or ""
        except Exception:
            pass
    return key, call


def _instanz(session_key: str):
    """The running plugin instance that serves *session_key* (attached by the board), or None."""
    from .. import runtime
    rt = runtime()
    with rt.lock:
        instances = list(rt.instances.values())
    for inst in reversed(instances):
        ablage = getattr(inst, "ansichten", None)
        if ablage is None:
            continue
        if inst.buffers.get(session_key) is not None or ablage.angehaengt(session_key):
            return inst
    return None


def verfuegbar() -> bool:
    """check_fn: only in sessions the Werkbank board attached (never elsewhere, never raises)."""
    try:
        key, _call = _current()
        return bool(key) and _instanz(key) is not None
    except Exception:
        return False


def _antwort(**fields: Any) -> str:
    return json.dumps(fields, ensure_ascii=False)


def ausfuehren(args: Any, *, inst: Any = None, session_key: Optional[str] = None,
               tool_call_id: Optional[str] = None, emit: Optional[Callable[..., Any]] = None) -> str:
    """The tool. ``{"ok": true, "id": …}`` or ``{"error": "<German message>", "fehler": [...]}``."""
    key, call = _current()
    key = session_key if session_key is not None else key
    call = tool_call_id if tool_call_id is not None else call
    inst = inst if inst is not None else (_instanz(key) if key else None)
    if not key or inst is None:
        return _antwort(error="ansicht_zeigen geht nur in einer Sitzung, die im Werkbank-Chat offen ist.")
    server = getattr(inst, "server", None)
    aufl = Aufloeser(key, inst.ansichten.projekt(key), getattr(server, "outbox", None),
                     getattr(server, "uploads", None))
    view, fehler = pruefen(args, aufl)
    if view is None:
        return _antwort(error=fehlertext(fehler), fehler=fehler[:MAX_FEHLER])
    buf = inst.buffers.get(key)
    session_id = getattr(buf, "session_id", None)
    mark = None
    watermark = getattr(getattr(inst, "host", None), "row_watermark", None)
    if callable(watermark) and session_id:
        try:
            mark = watermark(key, session_id)
        except Exception:
            mark = None
    try:
        entry = inst.ansichten.speichere(key, view, tool_call_id=call, session_id=session_id,
                                         mark=mark if isinstance(mark, int) else None)
    except OSError as exc:
        return _antwort(error=f"Ansicht nicht gezeigt: Hermes konnte sie nicht speichern ({type(exc).__name__}).")
    payload = {"ansicht": entry["ansicht"], "tool_call_id": call or None}
    sent = None
    try:
        if emit is None:
            from gateway.session_events import emit as core_emit
            emit = core_emit
        sent = emit(key, session_id, None, EVENT, id=entry["id"], parent_id=call or None, payload=payload)
    except Exception:
        sent = None
    if sent is None:
        # nobody listens through the core (events off): the plugin's own buffer still gets it
        inst.buffers.on_event({"session_key": key, "session_id": session_id, "type": EVENT, "id": entry["id"],
                               "parent_id": call or None, "origin": None, "payload": payload})
    return _antwort(ok=True, id=entry["id"])


def _handler(args: Any, **_kw: Any) -> str:
    try:
        return ausfuehren(args)
    except Exception as exc:                      # never a traceback into the turn
        logger.warning("werkbank: ansicht_zeigen failed (%s)", type(exc).__name__)
        return _antwort(error=f"Ansicht nicht gezeigt: interner Fehler ({type(exc).__name__}).")


def registriere(ctx: Any) -> None:
    """Register the tool in the ``werkbank`` toolset; ``verfuegbar`` gates it per session."""
    try:
        from tools.registry import no_cache_check_fn
        check = no_cache_check_fn(verfuegbar)
    except Exception:
        check = verfuegbar
    try:
        desc = beschreibung()
        ctx.register_tool(name=TOOL_NAME, toolset=TOOLSET, schema=schema(), handler=_handler,
                          check_fn=check, description=desc, emoji="\U0001f4ca")
    except Exception:
        logger.warning("werkbank: ansicht_zeigen not registered", exc_info=True)

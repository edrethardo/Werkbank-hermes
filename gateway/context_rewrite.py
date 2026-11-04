"""WB-1426 (fork: werkbank-chat): the ONE way the owner rewrites a session's active transcript.

Used by the Werkbank context editor (WB-1319, ``plugins/platforms/werkbank/kontext.py``) and by the
gateway ``/clear`` (Telegram, Werkbank chat). Both surfaces of one session therefore share:

* **one lock per ``session_key``** over read → check → write (no lost update between an editor
  edit, ``/clear`` and their undos);
* **one write**: re-read and compare the fingerprint of what was read (``replace_messages`` has no
  compare-and-swap), then ``replace_messages(archive_dropped=True, reject_active_turn_lease=True)``
  — the dropped rows stay on disk (``active = 0``) and a turn lease held by ANY process (another
  gateway, TUI, CLI) refuses the write atomically;
* **one snapshot store** (``<home>/plugin-data/werkbank/kontext/<session_id>/<n>.json``, 0600 in 0700,
  newest five kept — the place WB-1319 introduced, kept so existing snapshots and the board's undo
  counter stay valid) and **one undo rule**: restore the newest snapshot only while the transcript
  still hashes to what the change left behind (``expected_after``), else a later turn would be
  dropped without the owner seeing it.

``/clear`` archives only the conversation (user/assistant/tool rows). The session row — system
prompt, model, profile, cwd, title — is not touched; ``restore_stored_prompt`` tells the next turn
to reuse the stored prompt although its history is empty (``agent/conversation_loop``), so the
provider's prompt cache keeps matching. No chat text is logged here.
"""

from __future__ import annotations

# fork: werkbank-chat — WB-1426: shared transcript rewrite (context editor + gateway /clear)
import copy
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("gateway.context_rewrite")

WRITE_STALE = "stale"          # write_messages' grund when the re-read no longer matches the base
SNAPSHOT_KEEP = 5
LOCK_WAIT_S = 10.0
HISTORY_ROLES = ("user", "assistant", "tool")
CLEAR_GRUND = "clear"          # the snapshot ``grund`` of a /clear
CLEARED_MARKER = "context_cleared"   # session metadata: the session id whose context /clear emptied

LEASE_REFUSED = "Ein Zug hält gerade den Verlauf — der Kontext ist schreibgeschützt."
MOVED_ON = ("Seit dieser Änderung ist der Verlauf weitergegangen — „Rückgängig“ würde die neueren "
            "Nachrichten verwerfen. Bitte von Hand bearbeiten.")

# the /clear texts (German like the rest of the werkbank-chat fork)
CLEAR_DONE = ("🧹 Kontext geleert ({n} Nachrichten archiviert). System-Prompt, Modell und "
              "Einstellungen bleiben. Rückgängig: /clear undo")
CLEAR_EXPIRED = "Offene Freigaben/Rückfragen sind damit verfallen."
CLEAR_UNDONE = "↩️ Kontext wiederhergestellt ({n} Nachrichten)."
CLEAR_EMPTY = "Der Kontext ist schon leer."
CLEAR_BUSY = "Ein Zug läuft gerade — erst /stop, dann /clear."
CLEAR_USAGE = ("/clear leert den Kontext dieser Sitzung (gleiche Sitzung, Verlauf wird archiviert). "
               "/clear undo stellt ihn bis zum nächsten Zug wieder her.")
CLEAR_MOVED_ON = ("Seit /clear ist der Verlauf weitergegangen — /clear undo würde die neueren "
                  "Nachrichten verwerfen.")
CLEAR_NOT_CLEAR = ("Der letzte Schnappschuss stammt nicht von /clear — zurücknehmen geht im "
                   "Kontext-Editor der Werkbank.")

_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class ContextRewriteError(Exception):
    """A refusal a surface may show: HTTP-ish ``status``, a machine ``error``, a German ``grund``."""

    def __init__(self, status: int, error: str, grund: str) -> None:
        super().__init__(grund)
        self.status = status
        self.error = error
        self.grund = grund


# ── lock ─────────────────────────────────────────────────────────────────────────────────────

def session_lock(session_key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(session_key)
        if lock is None:
            lock = _LOCKS[session_key] = threading.Lock()
        return lock


@contextmanager
def held(session_key: str, wait: Optional[float] = None):
    lock = session_lock(session_key)
    if not lock.acquire(timeout=LOCK_WAIT_S if wait is None else wait):
        raise ContextRewriteError(409, "busy", "Eine andere Änderung dieses Kontexts läuft gerade — "
                                               "bitte gleich noch einmal.")
    try:
        yield
    finally:
        lock.release()


# ── what the model sees ──────────────────────────────────────────────────────────────────────

def text_of(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(content)


def _call_ids(msg: Dict[str, Any]) -> List[str]:
    calls = msg.get("tool_calls")
    if not isinstance(calls, list):
        return []
    return sorted(c["id"] for c in calls if isinstance(c, dict) and isinstance(c.get("id"), str) and c["id"])


def fingerprint(messages: List[Dict[str, Any]]) -> str:
    """What the model sees of a transcript (role, text, tool identity) as one hash — bookkeeping
    keys (row ids, timestamps, markers) left out, so a re-read compares equal to what was written."""
    import hashlib

    parts = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        parts.append([str(msg.get("role") or ""), text_of(msg.get("content")), _call_ids(msg),
                      str(msg.get("tool_call_id") or "")])
    raw = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def estimate_tokens(prompt: Any, messages: Optional[List[Dict[str, Any]]] = None) -> int:
    """The core's rough estimate of system prompt + transcript (the ``≈`` number of the status line)."""
    try:
        from agent.model_metadata import estimate_tokens_rough
    except Exception:                                   # pragma: no cover - core always has it
        def estimate_tokens_rough(text):
            return len(text) // 4
    total = estimate_tokens_rough(prompt if isinstance(prompt, str) else "")
    for msg in messages or []:
        if isinstance(msg, dict):
            total += estimate_tokens_rough(text_of(msg.get("content")))
    return max(1, int(total))


# ── the database ─────────────────────────────────────────────────────────────────────────────

def session_db(owner: Any, session_key: str) -> Any:
    """The SessionDB serving *session_key* (profile-routed when the store can tell), unwrapped."""
    store = getattr(owner, "session_store", None)
    for_key = getattr(store, "_db_for_key", None)
    db = None
    if callable(for_key):
        try:
            db = for_key(session_key)
        except Exception:
            db = None
    if db is None:
        db = getattr(owner, "_session_db", None)
    return getattr(db, "_db", db)


def read_messages(db: Any, session_id: Optional[str]) -> List[Dict[str, Any]]:
    """The active transcript in the shape the next turn loads it — with row ids, the loader shape a
    rewrite wants (the SAME read ``/compress`` and the gateway's replay use)."""
    read = getattr(db, "get_messages_as_conversation", None)
    if not session_id or not callable(read):
        return []
    try:
        return [m for m in read(session_id, repair_alternation=True, include_row_ids=True)
                if isinstance(m, dict)]
    except Exception:
        logger.info("context rewrite: transcript not readable")
        return []


def write_messages(db: Any, session_id: Optional[str], messages: List[Dict[str, Any]], *,
                   expected: Optional[str] = None,
                   read: Optional[Callable[[], List[Dict[str, Any]]]] = None,
                   evict: Optional[Callable[[], None]] = None) -> Tuple[bool, str]:
    """Replace the active transcript rewind-style behind the DB turn-lease fence; then *evict*.

    ``expected`` = fingerprint of the transcript the change was made on: re-read right before the
    write, a mismatch answers ``(False, WRITE_STALE)`` and writes nothing."""
    write = getattr(db, "replace_messages", None)
    if not session_id or not callable(write):
        return False, "Diese Sitzung hat keinen schreibbaren Verlauf."
    if expected is not None:
        current = read() if read is not None else read_messages(db, session_id)
        if fingerprint(current or []) != expected:
            return False, WRITE_STALE
    try:
        write(session_id, messages, archive_dropped=True, reject_active_turn_lease=True)
    except Exception as exc:
        from hermes_state_errors import SessionTurnLeaseLostError
        if isinstance(exc, SessionTurnLeaseLostError):
            return False, LEASE_REFUSED
        logger.info("context rewrite: write refused (%s)", type(exc).__name__)
        return False, "Die Datenbank hat die Änderung abgelehnt."
    if evict is not None:
        try:
            evict()
        except Exception:
            logger.info("context rewrite: cached agent not evicted")
    return True, ""


def _refusal(grund: str) -> ContextRewriteError:
    if grund == WRITE_STALE:
        return ContextRewriteError(409, "stale", "Der Verlauf hat sich seit dem Lesen geändert — "
                                                 "bitte neu laden.")
    return ContextRewriteError(409, "write refused", grund or "Die Datenbank hat die Änderung abgelehnt.")


# ── snapshots ────────────────────────────────────────────────────────────────────────────────

def snapshot_root(home: Any) -> Path:
    """Where the snapshots of every surface live (the WB-1319 location, see the module doc)."""
    return Path(home) / "plugin-data" / "werkbank" / "kontext"


def _session_dir(root: Optional[Path], session_id: Optional[str]) -> Optional[Path]:
    if root is None or not isinstance(session_id, str) or not session_id \
            or "/" in session_id or "\\" in session_id:
        return None
    return Path(root) / session_id


def snapshots(root: Optional[Path], session_id: Optional[str]) -> List[Path]:
    folder = _session_dir(root, session_id)
    if folder is None or not folder.is_dir():
        return []
    return sorted((p for p in folder.iterdir() if p.suffix == ".json" and p.is_file()),
                  key=lambda p: p.name)


def newest_snapshot(root: Optional[Path], session_id: Optional[str]) -> Optional[Path]:
    found = snapshots(root, session_id)
    return found[-1] if found else None


def discard(path: Path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        logger.info("context rewrite: snapshot not removed")


def _now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def write_snapshot(root: Path, session_id: Optional[str], grund: str,
                   messages: List[Dict[str, Any]], expected_after: str = "") -> Path:
    """Store the state BEFORE a change: the whole active transcript, 0600, newest five kept."""
    folder = _session_dir(root, session_id)
    if folder is None:
        raise ContextRewriteError(409, "unknown session", "Diese Sitzung hat keine gültige Kennung.")
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    data = {"session_id": session_id, "grund": grund, "ts": _now(),
            # the state the change leaves behind: undo only while the history is still exactly that
            "expected_after": expected_after,
            "messages": [dict(m) for m in messages if isinstance(m, dict)]}
    name = f"{int(time.time() * 1000):015d}-{os.getpid()}.json"
    target = folder / name
    tmp = folder / (name + ".part")
    payload = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    for old in snapshots(root, session_id)[:-SNAPSHOT_KEEP]:
        discard(old)
    return target


def restore_newest(root: Optional[Path], session_id: Optional[str],
                   read: Callable[[], List[Dict[str, Any]]],
                   write: Callable[[List[Dict[str, Any]], str], None], *,
                   grund: Optional[str] = None, moved_on: str = MOVED_ON,
                   not_this_kind: str = CLEAR_NOT_CLEAR) -> int:
    """Restore the newest snapshot (and use it up); returns how many messages came back.

    ``write(messages, current_fingerprint)`` raises on refusal. ``grund`` restricts the undo to a
    snapshot of that kind (``/clear undo`` never takes back an editor change)."""
    newest = newest_snapshot(root, session_id)
    if newest is None:
        raise ContextRewriteError(409, "no snapshot", "Es gibt keinen Schnappschuss zum Zurücknehmen.")
    try:
        data = json.loads(newest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ContextRewriteError(500, "snapshot unreadable",
                                  "Der Schnappschuss ist nicht mehr lesbar.") from None
    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, list) or not messages:
        raise ContextRewriteError(500, "snapshot unreadable", "Der Schnappschuss enthält keinen Verlauf.")
    if grund is not None and data.get("grund") != grund:
        raise ContextRewriteError(409, "other snapshot", not_this_kind)
    expected = data.get("expected_after")
    current_fp = fingerprint(read() or [])
    if not isinstance(expected, str) or current_fp != expected:
        # a turn (or another edit) came after: restoring would drop it without the owner seeing it
        raise ContextRewriteError(409, "moved on", moved_on)
    write([dict(m) for m in messages if isinstance(m, dict)], current_fp)
    try:
        newest.unlink()
    except OSError:
        logger.info("context rewrite: snapshot not removed after undo")
    return len(messages)


# ── /clear ───────────────────────────────────────────────────────────────────────────────────

def clear_history(db: Any, session_key: str, session_id: Optional[str], root: Path, *,
                  evict: Optional[Callable[[], None]] = None) -> Dict[str, Any]:
    """Archive the conversation (user/assistant/tool) of the session; other rows stay. Snapshot first;
    a refused write removes it again. Returns ``{"archived": n, "snapshot": path}``."""
    with held(session_key):
        messages = read_messages(db, session_id)
        kept = [copy.deepcopy(m) for m in messages if m.get("role") not in HISTORY_ROLES]
        archived = len(messages) - len(kept)
        if archived <= 0:
            raise ContextRewriteError(409, "empty", CLEAR_EMPTY)
        snapshot = write_snapshot(root, session_id, CLEAR_GRUND, messages,
                                  expected_after=fingerprint(kept))
        ok, grund = write_messages(db, session_id, kept, expected=fingerprint(messages), evict=evict)
        if not ok:
            discard(snapshot)
            raise _refusal(grund)
        logger.info("context rewrite: context cleared (archived=%d)", archived)
        return {"archived": archived, "snapshot": snapshot}


def undo_clear(db: Any, session_key: str, session_id: Optional[str], root: Path, *,
               evict: Optional[Callable[[], None]] = None) -> int:
    """``/clear undo``: the newest snapshot, only when it is a /clear and nothing came after."""
    with held(session_key):
        def _write(msgs: List[Dict[str, Any]], expected: str) -> None:
            ok, grund = write_messages(db, session_id, msgs, expected=expected, evict=evict)
            if not ok:
                raise _refusal(grund)
        return restore_newest(root, session_id, lambda: read_messages(db, session_id), _write,
                              grund=CLEAR_GRUND, moved_on=CLEAR_MOVED_ON)


def restore_stored_prompt(store: Any, session_key: Optional[str], session_id: Optional[str]) -> bool:
    """True when /clear emptied THIS session's context: the next turn reuses the stored system
    prompt instead of building a new one for an empty history."""
    getter = getattr(store, "get_session_metadata", None)
    if not callable(getter) or not session_key or not session_id:
        return False
    try:
        return getter(session_key, CLEARED_MARKER) == session_id
    except Exception:
        return False

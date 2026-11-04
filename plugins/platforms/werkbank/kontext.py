"""WB-1319: the context editor of a chat session — inspect, edit (snapshot + undo), compress.

The editor shows what the NEXT turn would send: the system prompt (read-only) and the active
transcript of the session's SessionDB, in order, with an estimate per entry. Writes go through the
same rails ``/undo`` and ``/compress`` use (``replace_messages(archive_dropped=True,
reject_active_turn_lease=True)``), so the history on disk — not a copy in memory — changes and the
cached agent is evicted afterwards: the next turn rebuilds its prompt from what is written.

Rules of this module (they are the ticket's "Fertig, wenn" in code):

* **Nothing is half done.** A tool call and its result are one step. Removing either removes both;
  a transcript that already carries an incomplete pair is refused instead of being edited.
* **The system prompt is read-only.**
* **Snapshot before every change**, under ``<snapshots>/<session_id>/<n>.json`` (0600, directory
  0700, the newest five kept). ``undo`` restores the newest one and uses it up.
* **Owner and idle, server side.** ``host.write_gate`` answers for the gateway and its running
  turn. A writer in ANOTHER process (TUI, CLI on the same session) is not visible to it — this
  core has no ``_foreign_writer`` hook, so that check answers "" — the database's turn lease in
  ``replace_messages(reject_active_turn_lease=True)`` is the fence that holds there (atomic, but
  only while that process runs a turn).
* **Edit only what was seen whole.** A user/assistant text is editable only when the board's copy
  IS the stored text (not capped, nothing redacted, a plain string); else it can only be removed.
  ``edit`` carries ``base`` = sha256 of the whole stored text, a mismatch is ``409 stale``. A
  compression summary (``_compressed_summary``) is never editable, only removable.
* **No lost update.** edit/remove/shorten, undo and a real compression hold one lock per
  ``session_key`` over read → check → write; ``context_write`` gets the fingerprint of what was
  read and re-reads right before ``replace_messages`` (the core has no compare-and-swap) — a
  transcript that moved meanwhile is ``409 stale``, never silently overwritten.
* **Never a text with a secret.** Every string that leaves this module ran through the gateway's
  own egress redaction (``gateway.session_events._redact``).

No chat text is logged here.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# WB-1426: lock, fingerprint, write, snapshots and undo live in the core so the gateway's /clear
# (Telegram, Werkbank chat) takes the SAME way — one lock per session_key, one snapshot store
from gateway import context_rewrite as _core

logger = logging.getLogger("plugins.platforms.werkbank")

ITEM_TEXT_CAP = 8192          # bytes per entry in the list; the whole text via .../context/text/{i}
MAX_INSTRUCTION = 1000        # the focus topic (`/compress <focus>`) is one line of this length
MAX_TEXT = 100_000            # a rewritten message
MAX_OPS = 100                 # operations in one request
COMPRESS_TIMEOUT_S = 360.0    # one manual compression (a summariser call) may take minutes
SNAPSHOT_KEEP = _core.SNAPSHOT_KEEP
UNDO_HINT = ("Die Änderung bricht den Prompt-Cache: der nächste Zug schickt den geänderten Verlauf "
             "neu und ist deshalb teurer.")
ROLES = ("user", "assistant", "tool")

LOCK_WAIT_S = 10.0            # an edit waits this long for another change of the same session
WRITE_STALE = _core.WRITE_STALE   # context_write's grund when the re-read no longer matches the base

# one lock per session_key: read → check → write of edit/remove/shorten, undo and compression is
# one step, so two requests of the same session can never both read the old state (lost update).
# The core owns the lock (WB-1426): a /clear of the same session waits for it too.
_session_lock = _core.session_lock


@contextmanager
def _held(session_key: str, wait: Optional[float] = None):
    lock = _session_lock(session_key)
    if not lock.acquire(timeout=LOCK_WAIT_S if wait is None else wait):
        raise KontextFehler(409, "busy", "Eine andere Änderung dieses Kontexts läuft gerade — "
                                         "bitte gleich noch einmal.")
    try:
        yield
    finally:
        lock.release()


def _write(host: Any, session_key: str, session_id: Optional[str],
           messages: List[Dict[str, Any]], expected: str) -> None:
    """``context_write`` with the fingerprint of what was read; a moved transcript is ``409 stale``."""
    ok, grund = host.context_write(session_key, session_id, messages, expected=expected)
    if ok:
        return
    if grund == WRITE_STALE:
        raise KontextFehler(409, "stale", "Der Verlauf hat sich seit dem Lesen geändert — "
                                          "bitte neu laden.")
    raise KontextFehler(409, "write refused", grund or "Die Datenbank hat die Änderung abgelehnt.")


_CONTROL = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060]")


# A refusal the board may read: HTTP ``status``, a machine ``error`` and a German ``grund`` — the
# core's class, so a refusal of the shared way (lock busy, snapshot, undo) needs no translation.
KontextFehler = _core.ContextRewriteError


# ── small shared helpers ──────────────────────────────────────────────────────────────────────

def redact(text: Any) -> str:
    """The gateway's own egress scrub (never a second, weaker one of our own)."""
    try:
        from gateway.session_events import _redact
        return _redact(text)
    except Exception:
        return ""


def capped(value: Any, limit: int = ITEM_TEXT_CAP) -> Tuple[str, bool]:
    """`(redacted text of at most *limit* bytes, was anything cut)` — fail closed at a line boundary."""
    try:
        from gateway.session_events import _capped as core_capped
        return core_capped(value, limit)
    except Exception:
        text = redact(value)
        raw = text.encode("utf-8", "replace")
        if len(raw) <= limit:
            return text, False
        return raw[:limit].decode("utf-8", "ignore"), True


def estimate(text: Any) -> int:
    """The core's rough token estimate — the same counting the rest of Hermes uses."""
    try:
        from agent.model_metadata import estimate_tokens_rough
        return int(estimate_tokens_rough(text if isinstance(text, str) else str(text or "")))
    except Exception:
        return max(1, len(str(text or "")) // 4)


text_of = _core.text_of


def one_line(text: str, limit: int = 160) -> str:
    """A preview: whitespace folded, control/format characters gone, cut (never escaped)."""
    flat = " ".join(_CONTROL.sub(" ", text).split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _row_id(msg: Dict[str, Any]) -> Optional[int]:
    rid = msg.get("_row_id")
    return rid if isinstance(rid, int) and not isinstance(rid, bool) else None


def _calls(msg: Dict[str, Any]) -> List[Dict[str, Any]]:
    calls = msg.get("tool_calls")
    return [c for c in calls if isinstance(c, dict)] if isinstance(calls, list) else []


def _call_id(call: Dict[str, Any]) -> Optional[str]:
    ident = call.get("id")
    return ident if isinstance(ident, str) and ident else None


# What the model sees of a transcript as one hash (the core's: /clear compares the same way).
fingerprint = _core.fingerprint


def incomplete_pairs(messages: List[Dict[str, Any]]) -> List[str]:
    """``tool_call_id`` values whose result has no call before it (a broken step).

    An assistant call whose result is missing is NOT listed: a turn cut between the call and its
    result is a legal (if useless) transcript state, while a result without its call is not.
    """
    seen, broken = set(), []
    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            seen.update(cid for cid in (_call_id(c) for c in _calls(msg)) if cid)
        elif role == "tool":
            cid = msg.get("tool_call_id")
            if isinstance(cid, str) and cid and cid not in seen:
                broken.append(cid)
    return broken


# ── inspection ────────────────────────────────────────────────────────────────────────────────

def base_of(text: str) -> str:
    """The ``base`` of an edit: the sha256 of the entry's WHOLE stored text, unredacted. The board
    gets it with the whole text and sends it back; any other value means it edited something else."""
    import hashlib
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def edit_block(content: Any) -> str:
    """Why this content may NOT be rewritten from the board's view ("" = it may). The board only
    ever sees a capped, redacted copy: saving that back would cut the message or write the mask
    over the real value, so such entries can only be removed (WB-1319 review)."""
    if not isinstance(content, str):
        return "Die Nachricht besteht aus mehreren Teilen (z. B. Bildern) — nur Entfernen geht."
    _shown, cut = capped(content)
    if cut:
        return "Die Nachricht ist länger als die Anzeige — Bearbeiten würde sie kürzen, nur Entfernen geht."
    if redact(content) != content:
        return "Die Nachricht enthält ein geschwärztes Geheimnis — Bearbeiten würde es überschreiben, nur Entfernen geht."
    return ""


def _item(index: int, msg: Dict[str, Any], length: Optional[int],
          owner_of_call: Dict[str, int]) -> Dict[str, Any]:
    role = str(msg.get("role") or "")
    calls = _calls(msg)
    call_ids = [cid for cid in (_call_id(c) for c in calls) if cid]
    pair = None
    if role == "tool":
        cid = msg.get("tool_call_id")
        pair = cid if isinstance(cid, str) and cid else None
    elif calls and call_ids:
        pair = call_ids[0] if len(call_ids) == 1 else None
    raw = text_of(msg.get("content"))
    preview_source = raw
    if role == "assistant" and calls and not raw.strip():
        names = ", ".join(str((c.get("function") or {}).get("name") or c.get("name") or "?") for c in calls)
        raw = "(Werkzeug-Aufruf: " + names + ")"
        preview_source = names
    if msg.get("_compressed_summary"):
        role = "summary"
    if role not in ROLES + ("summary",):
        role = "system"                     # a plugin's own line: shown, never touched
    shown, truncated = capped(raw)
    tokens = estimate(raw)
    if calls:
        tokens += estimate(json.dumps([{k: c.get(k) for k in ("id", "function")} for c in calls],
                                      ensure_ascii=False, default=str))
    editable = "text" if role in ("user", "assistant") else ("shorten" if role == "tool" else "none")
    edit_grund = ""
    if editable == "text":
        edit_grund = edit_block(msg.get("content"))
        if edit_grund:
            editable = "remove"             # capped or redacted view: never saved back
    return {
        "i": index, "id": _row_id(msg), "role": role, "ts": msg.get("timestamp"),
        "tokens": max(1, tokens), "share": (max(1, tokens) / length) if length else None,
        "preview": one_line(redact(preview_source)) or ("(leer)"),
        "text": shown, "truncated": bool(truncated),
        "tool_call_id": msg.get("tool_call_id") if role == "tool" else None,
        "tool_name": msg.get("tool_name") if role == "tool" else None,
        "pair": pair, "editable": editable, "edit_grund": edit_grund,
    }


def items_of(messages: List[Dict[str, Any]], length: Optional[int]) -> List[Dict[str, Any]]:
    """Every context entry in order, with its estimate and its share of the model's window."""
    owner_of_call: Dict[str, int] = {}
    for msg in messages:
        if msg.get("role") == "assistant":
            rid = _row_id(msg) or 0
            for cid in (_call_id(c) for c in _calls(msg)):
                if cid and cid not in owner_of_call:
                    owner_of_call[cid] = rid
    return [_item(i, msg, length, owner_of_call) for i, msg in enumerate(messages)]


def system_block(prompt: str, agent: Any) -> Dict[str, Any]:
    """The system prompt as sections — the core's own cache tiers when they are the prompt we have."""
    if not isinstance(prompt, str) or not prompt.strip():
        return {"bekannt": False, "editable": False, "tokens": 0, "sections": [],
                "grund": "Hermes hat für diese Sitzung noch keinen System-Prompt gespeichert "
                         "(die Sitzung hatte noch keinen Zug)."}
    sections: List[Dict[str, Any]] = []
    titles = (("stable", "Persona und Regeln"), ("context", "Projekt-Kontext (Dateien, Umgebung)"),
              ("volatile", "Skills, Gedächtnis, Profil"))
    try:
        if agent is not None:
            from agent.system_prompt import build_system_prompt_parts
            parts = build_system_prompt_parts(agent) or {}
            joined = "\n\n".join(p for p in (parts.get(k) for k, _ in titles) if p and p.strip())
            if joined == prompt:                     # only when the tiers ARE this prompt
                for key, title in titles:
                    body = parts.get(key) or ""
                    if not body.strip():
                        continue
                    shown, truncated = capped(body)
                    sections.append({"title": title, "text": shown, "truncated": bool(truncated),
                                     "tokens": estimate(body)})
    except Exception:
        sections = []
    if not sections:
        shown, truncated = capped(prompt)
        sections = [{"title": "System-Prompt", "text": shown, "truncated": bool(truncated),
                     "tokens": estimate(prompt)}]
    return {"bekannt": True, "editable": False, "tokens": estimate(prompt), "sections": sections,
            "grund": ""}


def _numbers(position: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    session = (position or {}).get("session") or {}
    tokens, length = session.get("context_tokens"), session.get("context_length")
    threshold = session.get("compression_threshold")
    return {"tokens": tokens if isinstance(tokens, int) else None,
            "length": length if isinstance(length, int) else None,
            "percent": session.get("context_percent") if isinstance(session.get("context_percent"), int) else None,
            "threshold": threshold if isinstance(threshold, int) else None,
            "source": session.get("context_source") or ""}


def counts(snap_root: Optional[Path], session_id: Optional[str]) -> int:
    """How many snapshots of this session are waiting (the board's „Rückgängig“ button)."""
    return len(_snapshots(snap_root, session_id))


def inspect(host: Any, buffers: Any, snap_root: Optional[Path], session_key: str) -> Dict[str, Any]:
    position = buffers.position(session_key)
    if position is None:
        raise KontextFehler(404, "unknown session", "Diese Sitzung ist nicht (mehr) aufgelöst.")
    session_id = position.get("session_id")
    numbers = _numbers(position)
    gate_ok, gate_grund = host.write_gate(session_key)
    running = bool(position.get("turn_id")) or bool(host.is_running(session_key))
    messages = [m for m in (host.context_messages(session_key, session_id) or []) if isinstance(m, dict)]
    items = items_of(messages, numbers["length"])
    prompt, agent = host.context_system(session_key, session_id)
    system = system_block(prompt, agent)
    broken = incomplete_pairs(messages)
    editable = bool(gate_ok) and not broken
    grund = "" if editable else (gate_grund if not gate_ok else
                                 "Die Sitzung trägt ein unvollständiges Werkzeug-Paar — bitte nicht "
                                 "bearbeiten, bis das behoben ist.")
    return {
        "session_id": session_id,
        "state": "arbeitet" if running else "wartet",
        "editable": editable, "grund": grund,
        "context": numbers,
        "system": system,
        "items": items,
        "estimate": {"items": sum(i["tokens"] for i in items), "system": system["tokens"],
                     "total": sum(i["tokens"] for i in items) + system["tokens"]},
        "incomplete_pairs": broken[:20],
        "undo": counts(snap_root, session_id),
        "hinweis": UNDO_HINT,
    }


def item_text(host: Any, buffers: Any, session_key: str, index: int) -> Dict[str, Any]:
    """The whole text of one entry (the list shows only its first bytes)."""
    position = buffers.position(session_key)
    if position is None:
        raise KontextFehler(404, "unknown session", "Diese Sitzung ist nicht (mehr) aufgelöst.")
    messages = host.context_messages(session_key, position.get("session_id")) or []
    if not isinstance(index, int) or index < 0 or index >= len(messages):
        raise KontextFehler(404, "unknown item", "Diesen Eintrag gibt es nicht (mehr) — bitte neu laden.")
    msg = messages[index]
    raw = text_of(msg.get("content"))
    whole, truncated = capped(raw, 1_000_000)
    role = str(msg.get("role") or "")
    grund = edit_block(msg.get("content")) if role in ("user", "assistant") else "nur Entfernen/Kürzen"
    editable = role in ("user", "assistant") and not msg.get("_compressed_summary") and not grund
    return {"i": index, "role": role, "text": whole,
            "truncated": bool(truncated), "tokens": estimate(raw),
            # the base only for an editable entry: there the shown text IS the stored text
            "editable": editable, "base": base_of(raw) if editable else None,
            "edit_grund": "" if editable else grund}


# ── editing ──────────────────────────────────────────────────────────────────────────────────

def _op_list(ops: Any) -> List[Dict[str, Any]]:
    if not isinstance(ops, list) or not ops:
        raise KontextFehler(400, "bad ops", "Es fehlt die Änderung (ops).")
    if len(ops) > MAX_OPS:
        raise KontextFehler(400, "bad ops", f"Höchstens {MAX_OPS} Änderungen auf einmal.")
    return [op for op in ops if isinstance(op, dict)]


def _find(messages: List[Dict[str, Any]], row_id: Any) -> int:
    if not isinstance(row_id, int) or isinstance(row_id, bool):
        raise KontextFehler(400, "bad id", "Jeder Eintrag braucht seine id.")
    for i, msg in enumerate(messages):
        if _row_id(msg) == row_id:
            return i
    raise KontextFehler(409, "stale", "Der Eintrag gehört nicht mehr zum Verlauf — bitte neu laden.")


def _drop(messages: List[Dict[str, Any]], index: int) -> int:
    """Remove the entry AND its tool partner; returns how many entries went (never 1 for a pair)."""
    msg = messages[index]
    role = msg.get("role")
    drop = {index}
    if role == "tool":
        cid = msg.get("tool_call_id")
        for j, other in enumerate(messages):
            if other.get("role") != "assistant" or j == index:
                continue
            calls = _calls(other)
            if not any(_call_id(c) == cid for c in calls):
                continue
            rest = [c for c in calls if _call_id(c) != cid]
            if rest:
                other["tool_calls"] = rest          # its other steps stay, this one goes
            elif text_of(other.get("content")).strip():
                other.pop("tool_calls", None)       # the text stays, the call goes
            else:
                drop.add(j)                        # nothing left: the row goes with it
            break
    elif role == "assistant":
        call_ids = {cid for cid in (_call_id(c) for c in _calls(msg)) if cid}
        for j, other in enumerate(messages):
            if other.get("role") == "tool" and other.get("tool_call_id") in call_ids:
                drop.add(j)
    for j in sorted(drop, reverse=True):
        messages.pop(j)
    return len(drop)


def _one_op(messages: List[Dict[str, Any]], op: Dict[str, Any]) -> Tuple[str, int]:
    """Apply one operation in place; returns ``("removed", rows_gone)`` / ``("changed", 1)``."""
    kind = op.get("op")
    index = _find(messages, op.get("id"))
    msg = messages[index]
    role = msg.get("role")
    if kind == "remove":
        if role not in ROLES:
            raise KontextFehler(409, "read only", "Der System-Prompt ist nur lesbar.")
        return "removed", _drop(messages, index)
    if kind == "edit":
        if role not in ("user", "assistant"):
            raise KontextFehler(409, "tool output",
                                "Werkzeug-Ausgaben lassen sich nur kürzen oder entfernen.")
        if msg.get("_compressed_summary"):
            raise KontextFehler(409, "not editable",
                                "Die Zusammenfassung einer Kompression lässt sich nur entfernen.")
        block = edit_block(msg.get("content"))
        if block:
            raise KontextFehler(409, "not editable", block)
        base = op.get("base")
        if not isinstance(base, str) or base != base_of(text_of(msg.get("content"))):
            raise KontextFehler(409, "stale", "Der bearbeitete Text ist nicht (mehr) der gespeicherte "
                                              "— bitte neu laden und den ganzen Text bearbeiten.")
        text = op.get("text")
        if not isinstance(text, str) or not text.strip():
            raise KontextFehler(400, "bad text", "Der neue Text fehlt.")
        if len(text) > MAX_TEXT:
            raise KontextFehler(400, "bad text", f"Höchstens {MAX_TEXT} Zeichen.")
        msg["content"] = text
        msg.pop("api_content", None)               # the old wire bytes must not survive an edit
        return "changed", 1
    if kind == "shorten":
        if role != "tool":
            raise KontextFehler(409, "not a tool output",
                                "Kürzen geht nur bei Werkzeug-Ausgaben.")
        raw = text_of(msg.get("content"))
        try:
            keep = int(op.get("max_chars"))
        except (TypeError, ValueError):
            raise KontextFehler(400, "bad max_chars", "max_chars fehlt.") from None
        keep = max(1, min(keep, ITEM_TEXT_CAP))
        if len(raw) <= keep:
            raise KontextFehler(409, "nothing to shorten", "Diese Ausgabe ist schon so kurz.")
        msg["content"] = raw[:keep].rstrip() + "\n… [von der Werkbank gekürzt]"
        msg.pop("api_content", None)               # the old wire bytes must not survive a cut
        return "changed", 1
    raise KontextFehler(400, "bad op", "Unbekannte Änderung (remove, edit, shorten).")


def edit(host: Any, buffers: Any, snap_root: Optional[Path], session_key: str,
         ops: Any) -> Dict[str, Any]:
    if snap_root is None:
        raise KontextFehler(501, "not set up", "Der Kontext-Editor hat in diesem Profil keinen Ablageort.")
    with _held(session_key):
        return _edit(host, buffers, snap_root, session_key, ops)


def _edit(host: Any, buffers: Any, snap_root: Path, session_key: str,
          ops: Any) -> Dict[str, Any]:
    position = buffers.position(session_key)
    if position is None:
        raise KontextFehler(404, "unknown session", "Diese Sitzung ist nicht (mehr) aufgelöst.")
    gate_ok, gate_grund = host.write_gate(session_key)
    if not gate_ok:
        raise KontextFehler(409, "locked", gate_grund or "Der Kontext ist gerade schreibgeschützt.")
    session_id = position.get("session_id")
    messages = [copy.deepcopy(m) for m in (host.context_messages(session_key, session_id) or [])
                if isinstance(m, dict)]
    read_fp = fingerprint(messages)
    broken = incomplete_pairs(messages)
    if broken:
        raise KontextFehler(409, "tool pair",
                            "Der Verlauf trägt ein unvollständiges Werkzeug-Paar (" + broken[0] +
                            ") — erst reparieren, dann bearbeiten.")
    operations = _op_list(ops)
    removed = changed = 0
    for op in operations:
        outcome, rows = _one_op(messages, op)
        if outcome == "removed":
            removed += rows
        else:
            changed += rows
    if not removed and not changed:
        raise KontextFehler(400, "nothing to do", "Die Änderung ändert nichts.")
    if incomplete_pairs(messages):
        raise KontextFehler(409, "tool pair",
                            "So bliebe eine Werkzeug-Ausgabe ohne ihren Aufruf zurück — das Paar "
                            "geht nur zusammen.")
    snapshot = _write_snapshot(snap_root, session_id, "edit", host, session_key,
                               expected_after=fingerprint(messages))
    try:
        _write(host, session_key, session_id, messages, read_fp)
    except KontextFehler:
        _discard(snapshot)
        raise
    logger.info("werkbank: context edited (removed=%d, changed=%d)", removed, changed)
    return {"ok": True, "removed": removed, "changed": changed, "snapshot": str(snapshot),
            "hinweis": UNDO_HINT, "undo": counts(snap_root, session_id)}


def undo(host: Any, buffers: Any, snap_root: Optional[Path], session_key: str) -> Dict[str, Any]:
    if snap_root is None:
        raise KontextFehler(501, "not set up", "Der Kontext-Editor hat in diesem Profil keinen Ablageort.")
    with _held(session_key):
        return _undo(host, buffers, snap_root, session_key)


def _undo(host: Any, buffers: Any, snap_root: Path, session_key: str) -> Dict[str, Any]:
    position = buffers.position(session_key)
    if position is None:
        raise KontextFehler(404, "unknown session", "Diese Sitzung ist nicht (mehr) aufgelöst.")
    gate_ok, gate_grund = host.write_gate(session_key)
    if not gate_ok:
        raise KontextFehler(409, "locked", gate_grund or "Der Kontext ist gerade schreibgeschützt.")
    session_id = position.get("session_id")
    # the core's undo rule (WB-1426): newest snapshot, only while the history is what it left
    restored = _core.restore_newest(
        snap_root, session_id, lambda: host.context_messages(session_key, session_id) or [],
        lambda messages, current_fp: _write(host, session_key, session_id, messages, current_fp))
    logger.info("werkbank: context restored from a snapshot (%d messages)", restored)
    return {"ok": True, "restored": restored, "undo": counts(snap_root, session_id),
            "hinweis": UNDO_HINT}


# ── compression ──────────────────────────────────────────────────────────────────────────────

def instruction(value: Any) -> str:
    """The focus topic as ONE line of at most ``MAX_INSTRUCTION`` characters (a text line reaching
    an agent session may not fake a new instruction with a newline)."""
    if not isinstance(value, str):
        return ""
    flat = " ".join(_CONTROL.sub(" ", value).split())
    return flat[:MAX_INSTRUCTION]


def kept_turns(value: Any) -> Optional[int]:
    """``None`` = compress everything; 1..100 = keep the last N exchanges verbatim (``here N``)."""
    if value in (None, "", 0, "0"):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise KontextFehler(400, "bad keep", "„Letzte Züge behalten“ muss eine Zahl sein.") from None
    if n < 0:
        raise KontextFehler(400, "bad keep", "„Letzte Züge behalten“ muss eine Zahl sein.")
    return max(1, min(n, 100))


def compress(host: Any, buffers: Any, session_key: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Run the core's manual compression (the one ``/compress`` uses) with this session's params."""
    focus = instruction(body.get("instruction"))
    keep = kept_turns(body.get("keep"))
    preview = bool(body.get("preview"))
    if preview:
        return _compress(host, buffers, session_key, focus, keep, preview)
    # a real compression rewrites the transcript: the same per-session lock as the editor, so an
    # edit can neither be overwritten by it nor overwrite it
    with _held(session_key):
        return _compress(host, buffers, session_key, focus, keep, preview)


def _compress(host: Any, buffers: Any, session_key: str, focus: str, keep: Optional[int],
              preview: bool) -> Dict[str, Any]:
    if not preview:
        gate_ok, gate_grund = host.write_gate(session_key)
        if not gate_ok:
            raise KontextFehler(409, "locked", gate_grund or "Der Kontext ist gerade schreibgeschützt.")
    result = host.compress(session_key, focus, keep, preview)
    if not isinstance(result, dict):
        raise KontextFehler(501, "not implemented",
                            "Dieses Hermes kann den Kontext nicht über das Plugin komprimieren "
                            "(der Kern hat keinen Kompressions-Weg für Plugins).")
    status = str(result.get("status") or "")
    if status not in ("preview", "compressed"):
        raise KontextFehler(409, status or "failed",
                            result.get("grund") or "Die Kompression hat nichts geändert.")
    before = result.get("before_tokens") if isinstance(result.get("before_tokens"), int) else 0
    after = result.get("after_tokens") if isinstance(result.get("after_tokens"), int) else 0
    if status == "compressed" and after > 0:
        # the status line shows the compressor's own numbers (WB-1309), marked as their source
        buffers.set_context(session_key, {"context_tokens": after}, source="kompression")
    return {"ok": True, "preview": preview, "before": before, "after": after,
            "removed": int(result.get("removed") or 0), "kept": int(result.get("kept") or 0),
            "lines": [str(line) for line in (result.get("lines") or [])][:20],
            "focus_topic": focus, "keep": keep,
            "context": _numbers(buffers.position(session_key))}


# ── snapshots ────────────────────────────────────────────────────────────────────────────────

# The snapshot store is the core's (WB-1426): a /clear snapshot of the same session counts here too.
_snapshots = _core.snapshots
_newest_snapshot = _core.newest_snapshot
_discard = _core.discard


def _write_snapshot(snap_root: Path, session_id: Optional[str], grund: str, host: Any,
                    session_key: str, expected_after: str = "") -> Path:
    """Store the state BEFORE the change: the whole active transcript, 0600, newest five kept."""
    messages = host.context_messages(session_key, session_id) or []
    return _core.write_snapshot(snap_root, session_id, grund, messages, expected_after=expected_after)

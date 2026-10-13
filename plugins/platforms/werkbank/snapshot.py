"""``GET /v1/s/{key}/snapshot``: where the stream stands plus the history to draw before it.

Field names are the ones the Werkbank chat view reads (``hermes_view.js`` ``hvSnapshot``, WB-1132;
board ``docs/plugins.md`` "The Hermes chat view")::

    {epoch, seq, session_key, session_id, state: "arbeitet"|"wartet", turn_id,
     messages: [{id, role: user|assistant|tool, text, origin, ts, tool_call_id, tool_name,
                 is_error, truncated, message_id?, tool_calls?}],
     pending_approvals: [approval.request payload shape],
     pending_clarify:   [clarify.request payload shape],
     subagents: [{id, parent_id, status, goal?, …}],
     session: {model, context_percent, tool_count, errors}}

Order matters for "the snapshot fits the stream": the buffer position (``epoch``, ``seq``) is
taken FIRST, the history is read AFTER. Anything that lands in the SessionDB in between is in
the history and may come again as an event after ``seq`` — the board de-duplicates by id. Nothing
falls between the two.

History comes from the session's SessionDB through its own read API (``get_messages``), never
from the database file; open approvals from ``tools.approval.list_gateway_approvals``, the open
clarify prompt from ``tools.clarify_gateway.get_pending_for_session`` (both via the host).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

HISTORY_LIMIT = 200          # newest messages in a snapshot
TOOL_TEXT_CAP = 4096         # tool output in the history, like tool.end (full text: tools/{id}/output)
STATE_RUNNING = "arbeitet"   # hvSnapshot: state === "arbeitet" marks a running turn
STATE_IDLE = "wartet"


def _capped(value: Any, limit: int = TOOL_TEXT_CAP):
    try:
        from gateway.session_events import _capped as core_capped
        return core_capped(value, limit)
    except Exception:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        raw = text.encode("utf-8")
        if len(raw) <= limit:
            return text, False
        return raw[:limit].decode("utf-8", errors="ignore"), True


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):          # multimodal parts
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False, default=str)


def _tool_failed(tool_name: Any, content: Any) -> bool:
    try:
        from agent.display import _detect_tool_failure
        return bool(_detect_tool_failure(str(tool_name or ""), content)[0])
    except Exception:
        return False


def message_item(row: Dict[str, Any], origin: Optional[str] = None,
                 client_message_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """One SessionDB row as the board draws it; ``None`` for rows it never shows."""
    role = row.get("role")
    if role not in ("user", "assistant", "tool"):
        return None
    db_id = row.get("id")
    platform_id = str(row["platform_message_id"]) if row.get("platform_message_id") else None
    # The same id the stream gives this message (session_events._message_ref): the sender's own
    # id, else the platform's, and only when both are missing the row id as "db:<id>".
    item: Dict[str, Any] = {"id": client_message_id or platform_id or f"db:{db_id}",
                            "client_message_id": client_message_id, "message_id": platform_id,
                            "db_id": db_id, "role": role, "ts": row.get("timestamp"),
                            "origin": origin, "tool_call_id": None, "tool_name": None,
                            "is_error": False, "truncated": False}
    raw = _text(row.get("content"))
    if role == "tool":
        text, truncated = _capped(raw)
        item.update(text=text, truncated=truncated, tool_call_id=row.get("tool_call_id"),
                    tool_name=row.get("tool_name"), is_error=_tool_failed(row.get("tool_name"), raw))
    else:
        item["text"] = raw
    calls = row.get("tool_calls")
    if role == "assistant" and isinstance(calls, list) and calls:
        item["tool_calls"] = [
            {"id": c.get("id"), "name": (c.get("function") or {}).get("name") or c.get("name")}
            for c in calls if isinstance(c, dict)
        ]
    return item


def _epoch_seconds(ts: Any) -> Optional[float]:
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str) and ts:
        from datetime import datetime
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _redacted(text: str) -> str:
    try:
        from gateway.session_events import _redact
        return _redact(text)
    except Exception:
        return text


def _complete(messages: List[Dict[str, Any]], rows: List[Dict[str, Any]], pos: Dict[str, Any],
              window_full: bool) -> None:
    """Make the snapshot complete up to its ``seq`` (contract, PROTOCOL.md "Snapshot"):

    * a ``user.message`` the stream already announced (accepted, steered, queued) whose SessionDB
      row is not written yet is added from the plugin buffer, under the stream's id;
    * the final answer of a ``turn.done`` whose assistant row is not in the SessionDB yet is added
      from the plugin buffer (id = the turn id), so the re-sync after ``turn.done`` shows it.

    A row that is already there (same id; or an unnamed row whose text ends with the announced text)
    is not doubled.
    Entries older than the history window are left out when the window is full.
    """
    oldest = _epoch_seconds(rows[0].get("timestamp")) if rows and window_full else None
    have_ids = {m["id"] for m in messages}
    # Rows the id map could not name ("db:<n>") may still be an announced message (a prefixed text);
    # each such row stands for at most one announced message, newest rows first.
    # Only a message without a platform id can have such a row (a platform id is stored with it),
    # and only a row written after the message was announced (60 s clock slack).
    unnamed = [((m.get("text") or "").strip(), _epoch_seconds(m.get("ts"))) for m in messages
               if m["role"] == "user" and str(m["id"]).startswith("db:")]
    answer_texts = {_redacted((m.get("text") or "")).strip() for m in messages if m["role"] == "assistant"}
    answer_texts |= {(m.get("text") or "").strip() for m in messages if m["role"] == "assistant"}

    def too_old(ts) -> bool:
        seconds = _epoch_seconds(ts)
        return oldest is not None and seconds is not None and seconds < oldest

    for user in pos.get("recent_users") or []:
        board_id = user.get("client_message_id") or user.get("id") or user.get("message_id")
        text = (user.get("text") or "").strip()
        if not board_id or board_id in have_ids or too_old(user.get("ts")):
            continue
        announced = _epoch_seconds(user.get("ts"))
        hit = None if user.get("message_id") else next(
            (i for i in range(len(unnamed) - 1, -1, -1)
             if text and unnamed[i][0].endswith(text)
             and (announced is None or unnamed[i][1] is None or unnamed[i][1] >= announced - 60)), None)
        if hit is not None:
            del unnamed[hit]
            continue
        messages.append({"id": str(board_id), "client_message_id": user.get("client_message_id"),
                         "message_id": user.get("message_id"), "db_id": None, "role": "user",
                         "text": user.get("text") or "", "ts": user.get("ts"), "origin": user.get("origin"),
                         "delivery": user.get("delivery"), "synthetic": user.get("synthetic"),
                         "tool_call_id": None, "tool_name": None, "is_error": False, "truncated": False,
                         "from_buffer": True})
        have_ids.add(str(board_id))
    for answer in pos.get("recent_answers") or []:
        text = (answer.get("text") or "").strip()
        if not text or text in answer_texts or too_old(answer.get("ts")):
            continue
        answer_id = answer.get("turn_id") or f"turn:{len(messages)}"
        messages.append({"id": str(answer_id), "client_message_id": None, "message_id": None, "db_id": None,
                         "role": "assistant", "text": answer.get("text"), "ts": answer.get("ts"),
                         "origin": None, "tool_call_id": None, "tool_name": None, "is_error": False,
                         "truncated": False, "from_buffer": True})
        answer_texts.add(text)


def build(buffers, host, session_key: str, idmap=None) -> Optional[Dict[str, Any]]:
    if buffers.get(session_key) is None:
        return None
    current = host.current_session_id(session_key)
    if current:
        buffers.watch(session_key, current, None)     # a changed id starts a new epoch first
    pos = buffers.position(session_key)
    if pos is None:
        return None
    session_id = pos["session_id"]
    rows = [r for r in host.history(session_key, session_id, HISTORY_LIMIT) or [] if isinstance(r, dict)]
    # Board messages have no platform id in the DB: the plugin's id map names them (idmap.py).
    users = [r for r in rows if r.get("role") == "user" and not r.get("platform_message_id")]
    ids = idmap.assign(session_key, [_text(r.get("content")) for r in users]) if idmap else []
    by_row = {id(r): cmid for r, cmid in zip(users, ids) if cmid}
    messages: List[Dict[str, Any]] = []
    for row in rows:
        item = message_item(row, pos["platform"], by_row.get(id(row)))
        if item is not None:
            if item["role"] == "user" and item["client_message_id"]:
                item["origin"] = "werkbank"
            messages.append(item)
    _complete(messages, rows, pos, len(rows) >= HISTORY_LIMIT)
    running = bool(pos["turn_id"]) or bool(host.is_running(session_key))
    session = dict(pos["session"])
    session.setdefault("context_percent", None)
    for name, value in (host.session_state(session_key) or {}).items():
        if value is not None:
            session[name] = value
    return {
        "epoch": pos["epoch"], "seq": pos["seq"], "session_key": session_key, "session_id": session_id,
        "state": STATE_RUNNING if running else STATE_IDLE, "turn_id": pos["turn_id"],
        "messages": messages,
        "pending_approvals": list(host.pending_approvals(session_key) or []),
        "pending_clarify": list(host.pending_clarify(session_key) or []),
        "subagents": pos["subagents"],
        "session": session,
    }

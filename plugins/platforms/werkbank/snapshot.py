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


def row_client_message_id(row: Dict[str, Any]) -> Optional[str]:
    """The sender's own id the gateway stored on a plugin-injected user row
    (``display_metadata.client_message_id``, ``gateway/run_plugin_injection.client_message_metadata``)."""
    meta = row.get("display_metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = None
    cmid = meta.get("client_message_id") if isinstance(meta, dict) else None
    return cmid if isinstance(cmid, str) and cmid else None


def message_item(row: Dict[str, Any], origin: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """One SessionDB row as the board draws it; ``None`` for rows it never shows."""
    role = row.get("role")
    if role not in ("user", "assistant", "tool"):
        return None
    db_id = row.get("id")
    platform_id = str(row["platform_message_id"]) if row.get("platform_message_id") else None
    client_message_id = row_client_message_id(row) if role == "user" else None
    # The same id the stream gives this message (session_events._message_ref): the sender's own
    # id, else the platform's, and only when both are missing the row id as "db:<id>".
    item: Dict[str, Any] = {"id": client_message_id or platform_id or f"db:{db_id}",
                            "client_message_id": client_message_id, "message_id": platform_id,
                            "db_id": db_id, "role": role, "ts": row.get("timestamp"),
                            "origin": "werkbank" if client_message_id else origin,
                            "tool_call_id": None, "tool_name": None,
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


def _is_answer_row(row: Dict[str, Any]) -> bool:
    """An assistant row that can be a turn's final answer (text, no tool calls)."""
    return (row.get("role") == "assistant" and not row.get("tool_calls")
            and bool(_text(row.get("content")).strip()))


def _complete(messages: List[Dict[str, Any]], rows: List[Dict[str, Any]], pos: Dict[str, Any],
              window_full: bool) -> None:
    """Make the snapshot complete up to its ``seq`` (contract, PROTOCOL.md "Snapshot"):

    * a ``user.message`` the stream already announced (accepted, steered, queued) whose SessionDB
      row is not there yet is added from the plugin buffer, under the stream's id. A row stands
      for it only by that id (``client_message_id`` stored on the row, or the platform id) —
      never by its text or its time: an unnamed row and the buffered message are both shown;
    * the final answer of a ``turn.done`` is added from the plugin buffer (id = the turn id) until
      an answer row of THAT turn exists: an assistant text row written after the turn's
      ``turn.start`` and before the next ``turn.start`` (row timestamps, never the text).

    Entries older than the history window are left out when the window is full.
    """
    oldest = _epoch_seconds(rows[0].get("timestamp")) if rows and window_full else None
    have_ids = {m["id"] for m in messages}

    def too_old(ts) -> bool:
        seconds = _epoch_seconds(ts)
        return oldest is not None and seconds is not None and seconds < oldest

    for user in pos.get("recent_users") or []:
        board_id = user.get("client_message_id") or user.get("id") or user.get("message_id")
        if not board_id or str(board_id) in have_ids or too_old(user.get("ts")):
            continue
        messages.append({"id": str(board_id), "client_message_id": user.get("client_message_id"),
                         "message_id": user.get("message_id"), "db_id": None, "role": "user",
                         "text": user.get("text") or "", "ts": user.get("ts"), "origin": user.get("origin"),
                         "delivery": user.get("delivery"), "synthetic": user.get("synthetic"),
                         "tool_call_id": None, "tool_name": None, "is_error": False, "truncated": False,
                         "from_buffer": True})
        have_ids.add(str(board_id))

    answer_times = [t for t in (_epoch_seconds(r.get("timestamp")) for r in rows if _is_answer_row(r))
                    if t is not None]
    starts = sorted(t for t in (_epoch_seconds(s) for s in pos.get("turn_starts") or []) if t is not None)
    for answer in pos.get("recent_answers") or []:
        if not (answer.get("text") or "").strip() or too_old(answer.get("ts")):
            continue
        started = _epoch_seconds(answer.get("started"))
        if started is not None:
            following = next((t for t in starts if t > started), None)
            if any(t >= started and (following is None or t < following) for t in answer_times):
                continue                                   # this turn's answer row is in the history
        answer_id = answer.get("turn_id") or f"turn:{len(messages)}"
        messages.append({"id": str(answer_id), "client_message_id": None, "message_id": None, "db_id": None,
                         "role": "assistant", "text": answer.get("text"), "ts": answer.get("ts"),
                         "origin": None, "tool_call_id": None, "tool_name": None, "is_error": False,
                         "truncated": False, "from_buffer": True})


def build(buffers, host, session_key: str) -> Optional[Dict[str, Any]]:
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
    messages: List[Dict[str, Any]] = []
    for row in rows:
        item = message_item(row, pos["platform"])
        if item is not None:
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

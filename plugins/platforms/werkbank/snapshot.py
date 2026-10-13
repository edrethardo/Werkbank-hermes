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


def _row_meta(row: Dict[str, Any], name: str) -> Optional[str]:
    meta = row.get("display_metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = None
    value = meta.get(name) if isinstance(meta, dict) else None
    return value if isinstance(value, str) and value else None


def row_client_message_id(row: Dict[str, Any]) -> Optional[str]:
    """The sender's own id the gateway stored on a plugin-injected user row
    (``display_metadata.client_message_id``, ``gateway/run_plugin_injection.client_message_metadata``)."""
    return _row_meta(row, "client_message_id")


def _row_origin(row: Dict[str, Any], origin: Optional[str], plugin_id: str) -> Optional[str]:
    """``werkbank`` for a row THIS plugin injected (``display_metadata.hermes_plugin_id``), another
    plugin's id for its rows — the names the stream uses (``Buffers.on_event``) — else *origin*."""
    injected_by = _row_meta(row, "hermes_plugin_id")
    if injected_by is None:
        return origin
    return "werkbank" if injected_by == plugin_id else injected_by


def message_item(row: Dict[str, Any], origin: Optional[str] = None,
                 plugin_id: str = "platforms/werkbank") -> Optional[Dict[str, Any]]:
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
                            "origin": _row_origin(row, origin, plugin_id) if role == "user" else origin,
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


def _row_id(row: Dict[str, Any]) -> Optional[int]:
    rid = row.get("id")
    return rid if isinstance(rid, int) and not isinstance(rid, bool) else None


def _is_answer_row(row: Dict[str, Any]) -> bool:
    """An assistant row that can be a turn's final answer (text, no tool calls)."""
    return (row.get("role") == "assistant" and not row.get("tool_calls")
            and bool(_text(row.get("content")).strip()))


def _row_uids(rows: List[Dict[str, Any]]) -> set:
    """Every ``message_uid`` the history rows stand for, the ones a merge folded into them included."""
    uids = set()
    for row in rows:
        if isinstance(row.get("message_uid"), str) and row["message_uid"]:
            uids.add(row["message_uid"])
        absorbed = row.get("absorbed_message_uids")
        if isinstance(absorbed, str):
            try:
                absorbed = json.loads(absorbed)
            except ValueError:
                absorbed = None
        if isinstance(absorbed, list):
            uids.update(u for u in absorbed if isinstance(u, str) and u)
    return uids


def _complete(messages: List[Dict[str, Any]], rows: List[Dict[str, Any]], pos: Dict[str, Any],
              window_full: bool, lineage: List[str]) -> None:
    """Make the snapshot complete up to its ``seq`` (contract, PROTOCOL.md "Snapshot").

    Only entries of the session shown count: its own id or, after a compression, an id of its
    compression lineage (*lineage*) — never those of a session ``/new`` left behind.

    * a ``user.message`` the stream already announced whose SessionDB row is not there yet is
      added from the plugin buffer, under the stream's id. A row stands for it only by that id
      (``client_message_id`` stored on the row, or the platform id) — never by text or time;
    * the final answer of a ``turn.done`` is added from the plugin buffer (id = the turn id) until
      its row is in the history:
        - the core named the row (``turn.done`` ``message_uid``): only a row with that uid counts;
        - else by row ids (they only grow): the answer row of a turn is an assistant text row with
          an id above the turn's mark — the highest row id at its ``turn.start``, or, for a turn
          begun before the plugin observed the session, at that observation — and not above the
          mark of the next ``turn.start``. Never the wall clock, never the text.

    With a full history window, entries whose row would lie before the window's first row are
    left out (decided by row ids too).
    """
    allowed = set(lineage)
    first_row = next((rid for rid in (_row_id(r) for r in rows) if rid is not None), None)
    low = first_row if window_full else None
    have_ids = {m["id"] for m in messages}

    for user in pos.get("recent_users") or []:
        board_id = user.get("client_message_id") or user.get("id") or user.get("message_id")
        if user.get("session_id") not in allowed or not board_id or str(board_id) in have_ids:
            continue
        mark = user.get("mark")
        if low is not None and isinstance(mark, int) and mark < low:
            continue                                       # its row may lie before the window
        messages.append({"id": str(board_id), "client_message_id": user.get("client_message_id"),
                         "message_id": user.get("message_id"), "db_id": None, "role": "user",
                         "text": user.get("text") or "", "ts": user.get("ts"), "origin": user.get("origin"),
                         "delivery": user.get("delivery"), "synthetic": user.get("synthetic"),
                         "tool_call_id": None, "tool_name": None, "is_error": False, "truncated": False,
                         "from_buffer": True})
        have_ids.add(str(board_id))

    answer_ids = [rid for rid in (_row_id(r) for r in rows if _is_answer_row(r)) if rid is not None]
    uids = _row_uids(rows)
    starts = sorted(pos.get("turn_starts") or [], key=lambda t: t.get("order") or 0)
    observed = pos.get("observed") or {}
    for answer in pos.get("recent_answers") or []:
        if answer.get("session_id") not in allowed or not (answer.get("text") or "").strip():
            continue
        if answer.get("message_uid"):
            if answer["message_uid"] in uids:
                continue                                   # its own row is in the history
            row_id = answer.get("row_id")
            if low is not None and isinstance(row_id, int) and row_id < low:
                continue                                   # its row lies before the window
        else:
            above = answer.get("start_mark") if answer.get("started") else observed.get(answer.get("session_id"))
            above = above if isinstance(above, int) else -1
            following = next((t.get("mark") for t in starts if (t.get("order") or 0) > (answer.get("order") or 0)
                              and isinstance(t.get("mark"), int)), None)
            if any(rid > above and (following is None or rid <= following) for rid in answer_ids):
                continue                                   # this turn's answer row is in the history
            if low is not None and following is not None and following < low:
                continue                                   # its row lies before the window
        answer_id = answer.get("turn_id") or f"turn:{len(messages)}"
        messages.append({"id": str(answer_id), "client_message_id": None, "message_id": None, "db_id": None,
                         "role": "assistant", "text": answer.get("text"), "ts": answer.get("ts"),
                         "origin": None, "tool_call_id": None, "tool_name": None, "is_error": False,
                         "truncated": False, "from_buffer": True})


def _lineage(host, session_key: str, session_id: Optional[str]) -> List[str]:
    """*session_id* and its compression ancestors (``SessionDB`` resume lineage), never a session
    that ``/new`` replaced."""
    ids = [session_id] if session_id else []
    lineage = getattr(host, "lineage", None)
    if callable(lineage) and session_id:
        try:
            ids += [str(s) for s in lineage(session_key, session_id) or [] if s]
        except Exception:
            pass
    return ids


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
        item = message_item(row, pos["platform"], buffers.plugin_id)
        if item is not None:
            messages.append(item)
    _complete(messages, rows, pos, len(rows) >= HISTORY_LIMIT, _lineage(host, session_key, session_id))
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

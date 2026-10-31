"""``GET /v1/s/{key}/snapshot``: where the stream stands plus the history to draw before it.

Field names are the ones the Werkbank chat view reads (``hermes_view.js`` ``hvSnapshot``, WB-1132;
board ``docs/plugins.md`` "The Hermes chat view")::

    {epoch, seq, session_key, session_id, state: "arbeitet"|"wartet", turn_id,
     messages: [{id, role: user|assistant|tool|system, text, origin, ts, tool_call_id, tool_name,
                 is_error, truncated, message_id?, tool_calls?, kind?: system, herkunft?}],
     pending_approvals: [approval.request payload shape],
     pending_clarify:   [clarify.request payload shape],
     subagents: [{id, parent_id, status, goal?, model?, provider?, …}],
     session: {model, context_percent, tool_count, errors, provider?, context_tokens?,
               context_length?, compression_threshold?}}

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
BOARD_ID_PREFIX = "wb-"      # client_message_id the board mints (hermes_view.js hvNewId: "wb-" + uuid)


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


# Gateway-made user rows (``display_kind``): never the owner's words — unless the row names a
# sender's own id (a board message is injected as an internal event, WB-1128).
INTERNAL_KINDS = frozenset({"hidden", "internal_notification", "async_delegation_complete",
                            "process_complete"})
FOLDED_KEY = "folded_messages"   # agent.folded_messages.FOLDED_MESSAGES_KEY (WB-1175)


def _meta(row: Dict[str, Any]) -> Dict[str, Any]:
    meta = row.get("display_metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = None
    return meta if isinstance(meta, dict) else {}


def _human_view(row: Dict[str, Any]) -> Optional[Any]:
    """The owner's part of a user row the core marked as a compaction handoff
    (``_compressed_summary``), by the core's own split; ``None`` when it holds none."""
    try:
        from agent.context_compressor import user_originated_turn_view
    except Exception:
        return row.get("content")                    # no core to ask: show it as it is
    view = user_originated_turn_view({"role": "user", "content": row.get("content"),
                                      "_compressed_summary": True})
    return None if view is None else view.get("content")


def _row_origin(row: Dict[str, Any], origin: Optional[str], plugin_id: str) -> Optional[str]:
    """``werkbank`` for a row THIS plugin injected (``display_metadata.hermes_plugin_id``), another
    plugin's id for its rows — the names the stream uses (``Buffers.on_event``) — else *origin*."""
    injected_by = _row_meta(row, "hermes_plugin_id")
    if injected_by is None:
        # a row of a core before hermes_plugin_id: the board's own ids (hermes_view.js hvNewId)
        cmid = row_client_message_id(row)
        return "werkbank" if cmid is not None and cmid.startswith(BOARD_ID_PREFIX) else origin
    return "werkbank" if injected_by == plugin_id else injected_by


def _system_item(item: Dict[str, Any], plugin: str) -> Dict[str, Any]:
    """*item* as a plugin's system line (WB-1182): its own words under the plugin's name, never a
    user item — a board before WB-1182 skips ``role: system`` and stays as it was."""
    return {**item, "role": "system", "kind": "system", "herkunft": plugin, "origin": plugin}


def message_items(row: Dict[str, Any], origin: Optional[str] = None,
                  plugin_id: str = "platforms/werkbank") -> List[Dict[str, Any]]:
    """The chat items one SessionDB row stands for (WB-1175), all by markers the core wrote:

    * a row that names the messages folded into it (``display_metadata.folded_messages``: steer,
      redirect, leftover steer) — one item per part, under the part's ``ref`` (the id its
      ``user.message`` was announced under), with the sender's own words; an internal part none;
    * a compaction handoff (``_compressed_summary``) — only the owner's part, else nothing;
    * a gateway-internal user row (``INTERNAL_KINDS``) a plugin injected without a sender's id
      (``display_metadata.hermes_plugin_id``, WB-1182) — one system line (:func:`_system_item`);
    * any other gateway-internal user row without a sender's id — nothing;
    * every other row — :func:`message_item`.
    """
    if row.get("role") == "user":
        meta = _meta(row)
        parts = meta.get(FOLDED_KEY)
        if isinstance(parts, list) and parts:
            return _folded_items(row, parts, origin, plugin_id)
        if row.get("_compressed_summary"):
            human = _human_view(row)
            if human is None or not _text(human).strip():
                return []
            row = {**row, "content": human}
        elif row.get("display_kind") in INTERNAL_KINDS and row_client_message_id(row) is None:
            plugin = _row_meta(row, "hermes_plugin_id")      # the injecting plugin (WB-1182)
            item = message_item(row, origin, plugin_id) if plugin else None
            return [_system_item(item, plugin)] if plugin and item is not None else []
    item = message_item(row, origin, plugin_id)
    return [] if item is None else [item]


def _folded_items(row: Dict[str, Any], parts: List[Any], origin: Optional[str],
                  plugin_id: str) -> List[Dict[str, Any]]:
    items = []
    for n, part in enumerate(parts, 1):
        if not isinstance(part, dict) or part.get("internal") or not isinstance(part.get("text"), str):
            continue
        ref = part.get("ref")
        ref = str(ref) if isinstance(ref, (str, int)) and not isinstance(ref, bool) and str(ref) else None
        cmid = part.get("client_message_id") if isinstance(part.get("client_message_id"), str) else None
        sender = {"display_metadata": {k: part[k] for k in ("client_message_id", "hermes_plugin_id")
                                       if isinstance(part.get(k), str) and part[k]}}
        item = {"id": ref or f"db:{row.get('id')}#{n}", "client_message_id": cmid,
                "message_id": ref if ref != cmid else None, "db_id": row.get("id"),
                "role": "user", "ts": row.get("timestamp"),
                "origin": _row_origin(sender, origin, plugin_id),
                "tool_call_id": None, "tool_name": None, "is_error": False, "truncated": False,
                "text": part["text"], **({"synthetic": part["synthetic"]}
                                         if isinstance(part.get("synthetic"), str) else {})}
        plugin = sender["display_metadata"].get("hermes_plugin_id")
        # a plugin's part without a sender's id (a wake order folded into the turn, WB-1182)
        items.append(_system_item(item, plugin) if plugin and not cmid else item)
    return items


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
    if role == "assistant" and isinstance(row.get("message_uid"), str) and row["message_uid"]:
        item["message_uid"] = row["message_uid"]          # WB-1321: the outbox finds the row's files by it
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
    """Every ``message_uid`` the visible history rows stand for, the ones a merge folded into them
    included. A row without visible text (an empty assistant row) stands for nothing."""
    uids = set()
    for row in rows:
        if not _text(row.get("content")).strip():
            continue
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


def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _complete(messages: List[Dict[str, Any]], rows: List[Dict[str, Any]], pos: Dict[str, Any],
              lineage: List[str]) -> Dict[str, List[int]]:
    """Make the snapshot complete up to its ``seq`` (contract, PROTOCOL.md "Snapshot"); returns the
    arrival orders of the buffered entries whose rows the history holds (``{"users", "answers"}``) —
    the caller drops them from the buffer for good (:meth:`buffer.Buffers.confirm`).

    Buffered entries only bridge the time until their SessionDB row exists. Only entries of the
    session shown count: its own id or, after a compression, an id of its compression lineage
    (*lineage*) — never those of a session ``/new`` left behind.

    * a ``user.message`` the stream already announced whose SessionDB row is not there yet is
      added from the plugin buffer, under the stream's id. A row stands for it only by that id
      (``client_message_id`` stored on the row, or the platform id) — never by text or time;
    * the final answer of a ``turn.done`` is added from the plugin buffer (id = the turn id) until
      its row is in the history:
        - the core named the row (``turn.done`` ``message_uid``): only a visible row with that uid
          counts;
        - else by row ids (they only grow): the answer row of a turn is an assistant text row with
          an id above the turn's mark — the highest row id at its ``turn.start``, or, for a turn
          begun before the plugin observed the session, at that observation — and not above the
          mark of the next ``turn.start``. Never the wall clock, never the text.
    * an entry whose row (``row_id``) or mark (> 0) lies below the first row of the active history is
      left out — whether the window is full or a compression / ``/undo`` replaced the rows: row
      ids only grow, so its row is no longer part of what the history shows;
    * only answers of turns after the newest confirmed answer are shown.
    """
    allowed = set(lineage)
    low = min((rid for rid in (_row_id(r) for r in rows) if rid is not None), default=None)

    def below(value: Any) -> bool:
        # a mark of 0 (the session had no row yet) says nothing: its rows may all lie above any row
        value = _int(value)
        return low is not None and value is not None and 0 < value < low

    row_ids = {m["id"] for m in messages}                  # ids the history rows stand for
    have_ids = set(row_ids)
    confirmed: Dict[str, List[int]] = {"users": [], "answers": []}
    waiting: List[tuple] = []                              # (mark, entry) not confirmed yet

    for user in pos.get("recent_users") or []:
        board_id = user.get("client_message_id") or user.get("id") or user.get("message_id")
        if user.get("session_id") not in allowed or not board_id:
            continue
        if str(board_id) in row_ids:
            if _int(user.get("order")) is not None:
                confirmed["users"].append(user["order"])   # its row is there: done for good
            continue
        if str(board_id) in have_ids:
            continue
        if below(user.get("mark")):
            continue                                       # its row lies before the active history
        waiting.append((_int(user.get("mark")),
                        {"id": str(board_id), "client_message_id": user.get("client_message_id"),
                         "message_id": user.get("message_id"), "db_id": None, "role": "user",
                         "text": user.get("text") or "", "ts": user.get("ts"), "origin": user.get("origin"),
                         "delivery": user.get("delivery"), "synthetic": user.get("synthetic"),
                         "tool_call_id": None, "tool_name": None, "is_error": False, "truncated": False,
                         "from_buffer": True}))
        have_ids.add(str(board_id))
    _place(messages, waiting)
    _place(messages, [(_int(c.get("mark")), _command_item(c)) for c in pos.get("recent_commands") or []
                      if c.get("session_id") in allowed and not below(c.get("mark"))])

    answer_ids = [rid for rid in (_row_id(r) for r in rows if _is_answer_row(r)) if rid is not None]
    uids = _row_uids(rows)
    starts = sorted(pos.get("turn_starts") or [], key=lambda t: t.get("order") or 0)
    observed = pos.get("observed") or {}
    confirmed_order = _int(pos.get("confirmed_order")) or 0
    shown: List[tuple] = []
    for answer in pos.get("recent_answers") or []:
        order = _int(answer.get("order")) or 0
        if (answer.get("session_id") not in allowed
                or not ((answer.get("text") or "").strip() or answer.get("files"))
                or order <= confirmed_order):
            continue
        if answer.get("message_uid"):
            if answer["message_uid"] in uids:
                confirmed["answers"].append(order)       # its own row is in the history
                continue
            if below(answer.get("row_id") if _int(answer.get("row_id")) is not None else answer.get("mark")):
                continue                                   # its row lies before the active history
        else:
            above = answer.get("start_mark") if answer.get("started") else observed.get(answer.get("session_id"))
            above = above if isinstance(above, int) else -1
            following = next((t.get("mark") for t in starts if (t.get("order") or 0) > order
                              and isinstance(t.get("mark"), int)), None)
            if any(rid > above and (following is None or rid <= following) for rid in answer_ids):
                confirmed["answers"].append(order)       # this turn's answer row is in the history
                continue
            if below(answer.get("mark") if (_int(answer.get("mark")) or 0) > 0 else following):
                continue                                   # its row lies before the active history
        shown.append((order, answer))
    newest = max(confirmed["answers"], default=0)          # older unconfirmed answers: not shown either
    for order, answer in shown:
        if order <= newest:
            continue
        answer_id = answer.get("turn_id") or f"turn:{order}"
        messages.append({"id": str(answer_id), "client_message_id": None, "message_id": None, "db_id": None,
                         "role": "assistant", "text": answer.get("text"), "ts": answer.get("ts"),
                         "origin": None, "tool_call_id": None, "tool_name": None, "is_error": False,
                         "truncated": False, "from_buffer": True,
                         **({"files": list(answer["files"])} if answer.get("files") else {})})   # WB-1321
    return confirmed


COMMAND_HERKUNFT = "befehl"   # hermes_view.js: a command's reply is a system line of this origin


def _command_item(entry: Dict[str, Any]) -> Dict[str, Any]:
    """WB-1256: a command's reply (``command.reply``) as a system line — it has no SessionDB row."""
    return {"id": entry.get("id"), "client_message_id": None, "message_id": None, "db_id": None,
            "role": "system", "kind": "system", "herkunft": COMMAND_HERKUNFT, "origin": "werkbank",
            "command": entry.get("command"), "text": entry.get("text") or "", "ts": entry.get("ts"),
            "tool_call_id": None, "tool_name": None, "is_error": bool(entry.get("refused")),
            "truncated": False, "from_buffer": True}


def _place(messages: List[Dict[str, Any]], waiting: List[tuple]) -> None:
    """Put each buffered entry where its row mark says it arrived (WB-1175): before the first
    history item whose row id lies above the mark — row ids only grow, the clock is never read.
    Without a mark it goes to the end, as before; equal marks keep their arrival order."""
    for mark, entry in waiting:
        at = len(messages)
        if mark is not None:
            for i, item in enumerate(messages):
                rid = _int(item.get("db_id"))
                if rid is not None and rid > mark and not item.get("from_buffer"):
                    at = i
                    break
            while at < len(messages) and messages[at].get("from_buffer"):
                at += 1                                    # behind earlier entries of that mark
        messages.insert(at, entry)


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


def _with_subagent(buffers, session_key: str, pending: Dict[str, Any]) -> Dict[str, Any]:
    """A child's open approval names its subagent (WB-1129), like ``approval.request`` in the stream."""
    if not isinstance(pending, dict) or pending.get("subagent_id"):
        return pending
    find = getattr(buffers, "subagent_for", None)
    sub = find(session_key, pending.get("child_session_id")) if callable(find) else None
    return {**pending, "subagent_id": sub} if sub else pending


def _pending_choices(host, session_key: str) -> List[Dict[str, Any]]:
    """WB-1256: open choice cards (``choice.request`` payload shape); none for a host without them."""
    pending = getattr(host, "pending_choices", None)
    if not callable(pending):
        return []
    try:
        return [p for p in pending(session_key) or [] if isinstance(p, dict)]
    except Exception:
        return []


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
        messages.extend(message_items(row, pos["platform"], buffers.plugin_id))
    confirmed = _complete(messages, rows, pos, _lineage(host, session_key, session_id))
    buffers.confirm(session_key, answers=confirmed["answers"], users=confirmed["users"])
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
        "pending_approvals": [_with_subagent(buffers, session_key, p)
                              for p in host.pending_approvals(session_key) or []],
        "pending_clarify": list(host.pending_clarify(session_key) or []),
        "pending_choices": list(_pending_choices(host, session_key)),
        "subagents": pos["subagents"],
        "session": session,
    }


KURZ_ROWS = 12            # WB-1388: newest rows the short read looks at (not the whole history)
KURZ_ROWS_WEIT = 200      # second, wider read when the newest rows are all tool rows
KURZ_TEXT = 200           # characters of the last message in the short read
KURZ_REDACT_BYTES = 2048  # window that runs through the redaction before the cut to KURZ_TEXT


def _ts_number(value: Any) -> Optional[float]:
    if isinstance(value, str) and value.strip():      # a buffered event carries an ISO time
        try:
            from datetime import datetime
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def kurz(buffers, host, session_key: str, uploads=None, outbox=None) -> Optional[Dict[str, Any]]:
    """WB-1388: ``GET /v1/s/{key}/kurz`` — where the chat stands, without the history.

    ``{epoch, seq, session_key, session_id, state, running, pending_approvals: n, pending_clarify: n,
    pending_choices: n, last_user_ts, last_assistant_ts, last: {role, text, ts, attachments: bool} | null}``

    Only the newest ``KURZ_ROWS`` rows are read (one bounded query), the chat list needs nothing
    more. The last message is redacted by the gateway's own egress scrub BEFORE it is cut to
    ``KURZ_TEXT`` characters. Buffered messages and answers the SessionDB has no row for yet count
    as in the snapshot; nothing is confirmed or dropped here (read only).
    """
    if buffers.get(session_key) is None:
        return None
    current = host.current_session_id(session_key)
    if current:
        buffers.watch(session_key, current, None)
    pos = buffers.position(session_key)
    if pos is None:
        return None
    session_id = pos["session_id"]
    running = bool(pos["turn_id"]) or bool(host.is_running(session_key))
    # a long tool run can fill the newest KURZ_ROWS rows; then read once more, wider
    for limit in (KURZ_ROWS, KURZ_ROWS_WEIT):
        rows = [r for r in host.history(session_key, session_id, limit) or [] if isinstance(r, dict)]
        messages: List[Dict[str, Any]] = []
        for row in rows:
            messages.extend(message_items(row, pos["platform"], buffers.plugin_id))
        _complete(messages, rows, pos, _lineage(host, session_key, session_id))   # read only: no confirm
        last_user = last_assistant = None
        last = None
        for item in messages:
            if item.get("role") not in ("user", "assistant") or item.get("kind") == "system":
                continue
            ts = _ts_number(item.get("ts"))
            if ts is not None:
                if item["role"] == "user":
                    last_user = ts if last_user is None else max(last_user, ts)
                else:
                    last_assistant = ts if last_assistant is None else max(last_assistant, ts)
            if str(item.get("text") or "").strip() or item.get("files"):
                last = item
        if last is not None or len(rows) < limit:
            break
    short = None
    if last is not None:
        raw = str(last.get("text") or "")
        attachments = bool(last.get("files"))
        if "MEDIA:" in raw:
            attachments = True
            raw = "\n".join(line for line in raw.splitlines() if "MEDIA:" not in line)
        try:
            if last["role"] == "user" and uploads is not None:
                attachments = attachments or bool(uploads.attachments(session_key, last.get("client_message_id")))
            if last["role"] == "assistant" and outbox is not None:
                refs = [r for r in (last.get("message_uid"), last.get("id")) if r]
                attachments = attachments or bool(outbox.for_refs(session_key, refs))
        except Exception:
            pass
        try:
            from . import kontext
            text, _cut = kontext.capped(raw, KURZ_REDACT_BYTES)      # redacted first, fail closed
        except Exception:
            text = ""
        short = {"role": last["role"], "text": text[:KURZ_TEXT], "ts": _ts_number(last.get("ts")),
                 "attachments": attachments}
    return {
        "epoch": pos["epoch"], "seq": pos["seq"], "session_key": session_key, "session_id": session_id,
        "state": STATE_RUNNING if running else STATE_IDLE, "running": running,
        "pending_approvals": len(host.pending_approvals(session_key) or []),
        "pending_clarify": len(host.pending_clarify(session_key) or []),
        "pending_choices": len(_pending_choices(host, session_key)),
        "last_user_ts": last_user, "last_assistant_ts": last_assistant, "last": short,
    }

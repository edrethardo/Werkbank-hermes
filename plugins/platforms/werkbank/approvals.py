"""``POST /v1/s/{key}/approvals/{request_id}`` and ``POST /v1/s/{key}/clarify/{clarify_id}`` (WB-1129).

The board answers ONE request, named by its exact id — never "the oldest", never "all":

* an approval only when ``request_id`` is pending in THIS session
  (``tools.approval.list_gateway_approvals(session_key)``), else ``404`` and nothing is resolved;
* only a scope the Telegram card offers for it (the flags ``_approval_notify_sync`` passes on:
  ``allow_session``, ``allow_permanent``, ``smart_denied``), else ``400``;
* a subagent's request (``delegated_child``, stamped by the core): ``once`` or ``deny`` only. A
  parent's answer resolves the parent's request id and nothing else, so it never frees a child;
* resolved with ``resolve_gateway_approval(…, request_id=…, decided_by="werkbank")`` — the core
  resolves under its lock, so of a Telegram tap and a board click exactly one wins (``resolved``
  1 / 0). The answer carries the outcome the agent actually got (``choice``, ``decided_by``), the
  loser's too.

A clarify prompt: only when ``clarify_id`` belongs to THIS session (checked again atomically by the
core), the answer coerced like a typed Telegram reply (number or label of a choice, free text for an
open prompt), ``resolve_gateway_clarify(…, decided_by="werkbank", session_key=…)``.

Nothing here logs a command, a question or an answer.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

CHOICES = ("once", "session", "always", "deny")
CHILD_CHOICES = ("once", "deny")
DECIDED_BY = "werkbank"
MAX_ANSWER = 20_000

Answer = Tuple[int, Dict[str, Any]]


def _refuse(code: int, error: str, grund: str) -> Answer:
    return code, {"error": error, "grund": grund}


def offered_choices(data: Dict[str, Any]) -> Tuple[str, ...]:
    """The scopes the Telegram card offers for this request — and, for a subagent, once/deny only."""
    if data.get("delegated_child"):
        return CHILD_CHOICES
    if data.get("smart_denied"):
        return CHILD_CHOICES
    out = ["once"]
    if data.get("allow_session", True):
        out.append("session")
    if data.get("allow_permanent", True):
        out.append("always")
    out.append("deny")
    return tuple(out)


def answer_approval(session_key: str, request_id: str, body: Dict[str, Any]) -> Answer:
    from tools import approval

    choice = body.get("choice")
    if not isinstance(choice, str) or choice not in CHOICES:
        return _refuse(400, "bad choice", "choice muss once, session, always oder deny sein.")
    pending = next((d for d in approval.list_gateway_approvals(session_key)
                    if d.get("request_id") == request_id), None)
    if pending is None:
        decided = approval.gateway_approval_decision(session_key, request_id)
        if decided is not None:                 # answered a moment ago (Telegram won): say how
            return 200, {"resolved": 0, **decided}
        return _refuse(404, "unknown request", "Diese Freigabe wartet in dieser Sitzung nicht (mehr).")
    if choice not in offered_choices(pending):
        if pending.get("delegated_child"):
            return _refuse(400, "scope refused",
                           "Eine Freigabe für einen Subagenten gilt nur einmal: einmal erlauben oder ablehnen.")
        return _refuse(400, "scope refused", "Diese Freigabe bietet den Umfang „%s“ nicht an." % choice)
    count = approval.resolve_gateway_approval(session_key, choice, request_id=request_id,
                                              decided_by=DECIDED_BY)
    if count:
        return 200, {"resolved": 1, "choice": choice, "decided_by": DECIDED_BY}
    decided = approval.gateway_approval_decision(session_key, request_id)
    return 200, {"resolved": 0, **(decided or {"choice": None, "decided_by": None})}


def _clarify_entry(session_key: str, clarify_id: str) -> Optional[Any]:
    from tools import clarify_gateway
    with clarify_gateway._lock:
        if clarify_id not in (clarify_gateway._session_index.get(session_key) or []):
            return None
        entry = clarify_gateway._entries.get(clarify_id)
        return entry if entry is not None and entry.session_key == session_key else None


def answer_clarify(session_key: str, clarify_id: str, body: Dict[str, Any]) -> Answer:
    from tools import clarify_gateway

    text = body.get("answer")
    if not isinstance(text, str) or len(text) > MAX_ANSWER:
        return _refuse(400, "bad answer", "Bitte eine Antwort als Text senden.")
    entry = _clarify_entry(session_key, clarify_id)
    if entry is None:
        return _refuse(404, "unknown clarify", "Diese Rückfrage wartet in dieser Sitzung nicht (mehr).")
    if entry.event.is_set():
        return 200, {"resolved": False}
    if not text.strip():
        return _refuse(400, "bad answer", "Bitte eine Antwort als Text senden.")
    value, _why = clarify_gateway._coerce_text_response_detailed(entry, text)
    if value is None:
        return _refuse(400, "bad answer", "Diese Antwort passt zu keiner der angebotenen Möglichkeiten.")
    resolved = clarify_gateway.resolve_gateway_clarify(clarify_id, value, decided_by=DECIDED_BY,
                                                       session_key=session_key)
    return 200, {"resolved": bool(resolved)}

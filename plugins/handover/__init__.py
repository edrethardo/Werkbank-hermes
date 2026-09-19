"""``/handover`` — carry context across a session boundary.

``/handover`` summarizes the live conversation into a structured briefing and persists it in
profile-scoped plugin state. The first turn of the NEXT session gets it injected into the user
message via ``pre_llm_call``, so work survives ``/new``, a restart, or coming back tomorrow.

Scope, deliberately narrow: this plugin does **not** free the current context. In-session
compaction is ``/compress`` (structured template, focus argument, secret redaction, iterative
updates) and duplicating it here would mean two summaries and two bills for one turn. What
compaction cannot do is survive a session boundary — ``ContextCompressor.on_session_reset()``
drops its summary on ``/new`` — and that gap is this plugin's entire reason to exist.

The briefing rides the user-message injection channel, never the system prompt: the host
invariant is that the system prompt stays byte-stable for the life of a conversation.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import handover_core as core

logger = logging.getLogger(__name__)

PLUGIN_NAME = "handover"
_STATE_KEY = "pending"

# Latest transcript seen per session id, captured from the hook payloads. The slash handler
# runs outside a turn and has no other way to see the conversation.
_TRANSCRIPTS: Dict[str, List[Dict[str, str]]] = {}
_LAST_SESSION_ID = ""
_MAX_TRACKED_SESSIONS = 8

_ctx: Any = None

_HELP = """/handover — save a briefing for your next session.

  /handover                Summarize this session and save the briefing.
  /handover <focus>        Same, but steer the summary (e.g. /handover focus on the deploy).
  /handover show           Show the briefing that is waiting.
  /handover discard        Throw the pending briefing away.
  /handover help           This text.

The briefing is delivered to the first message of your next session (after /new, a restart,
or tomorrow). This session is left untouched — to free context here, use /compress."""


# ---------------------------------------------------------------------------
# transcript capture
# ---------------------------------------------------------------------------

def _remember(session_id: Optional[str], messages: Any, assistant_response: Any = None) -> None:
    global _LAST_SESSION_ID
    if not session_id:
        return
    transcript = core.normalize_transcript(messages)
    if assistant_response:
        text = core._content_text(assistant_response).strip()
        if text and (not transcript or transcript[-1].get("content") != text):
            transcript.append({"role": "assistant", "content": core._truncate(text, core.MAX_MESSAGE_CHARS)})
    if not transcript:
        return
    _TRANSCRIPTS[session_id] = transcript
    _LAST_SESSION_ID = session_id
    while len(_TRANSCRIPTS) > _MAX_TRACKED_SESSIONS:
        _TRANSCRIPTS.pop(next(iter(_TRANSCRIPTS)))


def on_pre_llm_call(*, session_id: str = "", conversation_history: Any = None, **_kw: Any):
    """Capture the transcript and deliver a pending handover to a new session's first turn."""
    _remember(session_id, conversation_history)
    record = _load()
    if record is None or not core.should_inject(record, session_id):
        return None
    _store(None)  # one-shot: a redelivered briefing would grow every later turn
    logger.info("handover: injecting briefing from session %s into %s",
                record.get("source_session_id"), session_id)
    return {"context": core.format_injection(record)}


def on_post_llm_call(*, session_id: str = "", conversation_history: Any = None,
                     assistant_response: Any = None, **_kw: Any) -> None:
    """Keep the captured transcript current, including the reply the loop just produced."""
    _remember(session_id, conversation_history, assistant_response)


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

def _load() -> Optional[Dict[str, Any]]:
    if _ctx is None:
        return None
    try:
        record = _ctx.state.get(_STATE_KEY)
    except Exception as exc:  # unreadable state must not break a turn
        logger.warning("handover: cannot read pending briefing: %s", exc)
        return None
    return record if core.is_usable_record(record) else None


def _store(record: Optional[Dict[str, Any]]) -> bool:
    if _ctx is None:
        return False
    try:
        _ctx.state.set(_STATE_KEY, record)
        return True
    except Exception as exc:
        logger.warning("handover: cannot persist briefing: %s", exc)
        return False


def _current_session_id() -> str:
    """Session id of the live conversation, or the most recently seen one."""
    if _ctx is not None:
        cli = getattr(getattr(_ctx, "_manager", None), "_cli_ref", None)
        session_id = getattr(cli, "session_id", "") if cli is not None else ""
        if session_id:
            return session_id
    return _LAST_SESSION_ID


def _live_transcript() -> List[Dict[str, str]]:
    """Transcript of the session being handed over."""
    session_id = _current_session_id()
    if session_id and session_id in _TRANSCRIPTS:
        return _TRANSCRIPTS[session_id]
    return _TRANSCRIPTS.get(_LAST_SESSION_ID, [])


# ---------------------------------------------------------------------------
# briefing
# ---------------------------------------------------------------------------

def _summarize(transcript: List[Dict[str, str]], focus: str) -> tuple[str, str, bool]:
    """Return ``(text, model, generated)``; falls back to a mechanical digest on failure."""
    if _ctx is None:
        return core.fallback_briefing(transcript, "no plugin context"), "", False
    try:
        result = _ctx.llm.complete(
            core.build_messages(transcript, focus), timeout=120, purpose="session handover")
    except Exception as exc:
        logger.warning("handover: summarization failed (%s); using mechanical digest", exc)
        return core.fallback_briefing(transcript, str(exc)[:120]), "", False
    text = (getattr(result, "text", "") or "").strip()
    if not text:
        return core.fallback_briefing(transcript, "empty model response"), "", False
    return text, getattr(result, "model", "") or "", True


def _prepare(focus: str) -> tuple[Optional[Dict[str, Any]], str]:
    """Build and persist the briefing. Returns ``(record, message)``."""
    session_id = _current_session_id()
    transcript = _live_transcript()
    if not transcript:
        return None, "Nothing to hand over — this session has no conversation yet."
    text, model, generated = _summarize(transcript, focus)
    if not text.strip():
        return None, "Nothing to hand over — the summary came back empty."
    record = core.make_record(
        text, source_session_id=session_id, model=model, generated=generated,
        created_at=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"))
    if not _store(record):
        return None, "Handover prepared but could not be saved."
    return record, ""


# ---------------------------------------------------------------------------
# slash command
# ---------------------------------------------------------------------------

def _cmd_show(_focus: str) -> str:
    record = _load()
    if not record:
        return "No handover is pending."
    head = f"Handover waiting for your next session, written {record.get('created_at') or 'at an unknown time'}"
    if not record.get("generated"):
        head += " — mechanical digest, the summarizing model was unavailable"
    return f"{head}:\n\n{record['text']}"


def _cmd_discard(_focus: str) -> str:
    if not _load():
        return "No handover is pending."
    return "Pending handover discarded." if _store(None) else "Could not discard the pending handover."


def _cmd_handover(focus: str) -> str:
    record, problem = _prepare(focus)
    if record is None:
        return problem
    return (f"{record['text']}\n\n"
            "— Saved. Your next session's first message is answered with this briefing. "
            "This session is unchanged; use /compress to free context here.")


_SUBCOMMANDS = {
    "show": _cmd_show, "status": _cmd_show,
    "discard": _cmd_discard, "clear": _cmd_discard, "drop": _cmd_discard,
}


def handle_slash(raw_args: str) -> Optional[str]:
    """``/handover [show|discard|help] [focus…]`` — anything else is summary focus text."""
    argv = (raw_args or "").strip().split()
    if argv and argv[0] in {"help", "-h", "--help"}:
        return _HELP
    handler = _SUBCOMMANDS.get(argv[0]) if argv else None
    if handler is not None:
        return handler(" ".join(argv[1:]))
    return _cmd_handover(" ".join(argv))


def register(ctx) -> None:
    global _ctx
    _ctx = ctx
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_command(
        "handover", handler=handle_slash, args_hint="[show|discard] [focus]",
        description="Save a briefing so your next session continues this work.")

"""Pure helpers for the handover plugin: transcript normalization, prompt and rendering.

Only ``redact`` reaches into Hermes (forced secret redaction on anything persisted); everything
else is a pure data transform, testable without a plugin host.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def redact(text: str) -> str:
    """Force-redact secrets. A handover is written to disk and replayed into a later session,
    so it must never carry a credential even if the user disabled redaction display-side."""
    try:
        from agent.redact import redact_sensitive_text
        return redact_sensitive_text(text, force=True)
    except Exception:  # never lose the handover because redaction is unavailable
        return text

# Per-message and whole-transcript caps. A handover is a briefing, not an archive: the
# transcript is only ever read once, by the summarizing model.
MAX_MESSAGE_CHARS = 2_000
MAX_TRANSCRIPT_MESSAGES = 80
MAX_TRANSCRIPT_CHARS = 24_000
# Cap on the stored handover itself. It is prepended to the next session's first user
# message, so an oversized one would tax the new context it is supposed to save.
MAX_HANDOVER_CHARS = 8_000

HANDOVER_SCHEMA_VERSION = 1

_ROLES_KEPT = ("user", "assistant")

SYSTEM_PROMPT = (
    "You write handover briefings between two sessions of the same AI agent. "
    "The next session starts with an empty context window and must continue the work "
    "without re-asking the user anything that is already known."
)

INSTRUCTIONS = """\
Write a handover briefing from the transcript below. Be specific and dense — names, paths,
commands, decisions, numbers. Drop pleasantries, tool chatter and anything already finished
and irrelevant. Use this exact structure, omitting a section only when it is genuinely empty:

## Auftrag
What the user is ultimately trying to achieve.

## Stand
What is already done, and what was decided (with the reason).

## Offen
What is still open, in priority order.

## Nächster Schritt
The single concrete action the next session should take first.

## Kontext
Paths, commands, IDs, constraints and user preferences the next session needs. Bullets.

Write in the language the user writes in. No preamble, no closing remarks."""


def _content_text(content: Any) -> str:
    """Flatten OpenAI-format content (string or multimodal part list) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif part.get("type") in ("image_url", "image"):
                    parts.append("[image]")
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return "" if content is None else str(content)


def _truncate(text: str, limit: int) -> str:
    """Head+tail truncation, never exceeding *limit* — the tail of a long message often
    carries the conclusion, and a marker that pushed the result back over the cap would
    defeat every caller that truncates to fit a budget."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    omitted = len(text) - head  # upper bound; the marker only shrinks as tail grows
    marker = f"\n[… {omitted} chars omitted …]\n"
    tail = max(0, limit - head - len(marker))
    if tail == 0:
        return text[:max(0, limit - len(marker))] + marker
    return text[:head] + f"\n[… {len(text) - head - tail} chars omitted …]\n" + text[-tail:]


def normalize_transcript(messages: Any) -> List[Dict[str, str]]:
    """Reduce a raw message list to the capped ``[{role, content}]`` the briefing is built from.

    System prompts are dropped (the next session builds its own) and tool rows are dropped
    (their outcome is visible in the assistant text that follows). Assistant rows that carry
    only tool calls collapse to a short marker so the tool loop stays visible without its bulk.
    """
    if not isinstance(messages, list):
        return []
    out: List[Dict[str, str]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in _ROLES_KEPT:
            continue
        text = _content_text(msg.get("content")).strip()
        if not text:
            if role == "assistant" and msg.get("tool_calls"):
                names = [
                    (tc.get("function") or {}).get("name", "tool")
                    for tc in msg["tool_calls"] if isinstance(tc, dict)
                ]
                text = "[used tools: " + ", ".join(n for n in names if n) + "]"
            if not text:
                continue
        out.append({"role": role, "content": _truncate(text, MAX_MESSAGE_CHARS)})
    return out[-MAX_TRANSCRIPT_MESSAGES:]


def render_transcript(transcript: List[Dict[str, str]]) -> str:
    """Render a normalized transcript as the text block handed to the summarizing model."""
    rendered = "\n\n".join(f"### {m['role']}\n{m['content']}" for m in transcript)
    return _truncate(rendered, MAX_TRANSCRIPT_CHARS)


def build_messages(transcript: List[Dict[str, str]], focus: str = "") -> List[Dict[str, str]]:
    """Chat messages for the summarizing call. ``focus`` steers what to keep."""
    focus_line = f"\nThe user asked you to focus the handover on: {focus.strip()}\n" if focus.strip() else ""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"{INSTRUCTIONS}{focus_line}\n\n--- TRANSCRIPT ---\n{render_transcript(transcript)}"
        )},
    ]


def fallback_briefing(transcript: List[Dict[str, str]], reason: str = "") -> str:
    """Mechanical briefing used when the summarizing model is unavailable.

    Losing the handover because a side-LLM call failed would be worse than handing the next
    session a verbatim tail of the conversation, so this never returns empty for a non-empty
    transcript.
    """
    if not transcript:
        return ""
    note = f"(automatische Zusammenfassung nicht verfügbar: {reason})" if reason else \
        "(automatische Zusammenfassung nicht verfügbar)"
    tail = transcript[-8:]
    body = "\n\n".join(f"**{m['role']}:** {_truncate(m['content'], 600)}" for m in tail)
    return f"## Kontext\n{note}\n\nLetzte Wortwechsel der vorigen Session:\n\n{body}"


# A handover exists to carry context ACROSS a session boundary — that is the one thing
# in-session compaction cannot do (`ContextCompressor.on_session_reset` drops its summary).
# Freeing the CURRENT context is `/compress`'s job, focus argument included; this plugin
# deliberately does not touch it.
MODE_AWAIT_NEW_SESSION = "await_new_session"


def make_record(
    text: str, *, source_session_id: str, created_at: str, model: str = "", generated: bool = True,
) -> Dict[str, Any]:
    """Build the persisted handover record (redacted, then capped).

    Redaction is forced: this record is written to disk and replayed into a later session, so it
    must never carry a credential that happened to appear in the transcript.
    """
    return {
        "version": HANDOVER_SCHEMA_VERSION,
        "text": _truncate(redact(text.strip()), MAX_HANDOVER_CHARS),
        "source_session_id": source_session_id or "",
        "created_at": created_at,
        "model": model or "",
        "generated": bool(generated),
        "mode": MODE_AWAIT_NEW_SESSION,
    }


def is_usable_record(record: Any) -> bool:
    """Whether a stored value is a handover record this version can consume."""
    return (
        isinstance(record, dict)
        and record.get("version") == HANDOVER_SCHEMA_VERSION
        and isinstance(record.get("text"), str)
        and bool(record["text"].strip())
    )


def should_inject(record: Any, session_id: Optional[str]) -> bool:
    """Whether *record* should be delivered to the turn starting in *session_id*.

    A handover is held back until a session OTHER than the one that wrote it asks. The writing
    session still has its full history (``/handover`` never clears anything) and must not be
    handed a summary of context it already holds.
    """
    if not is_usable_record(record):
        return False
    return (session_id or "") != record.get("source_session_id", "")


def format_injection(record: Dict[str, Any]) -> str:
    """The context block injected into the receiving session's first user message."""
    created = record.get("created_at") or "unknown"
    return (
        f"[Session handover — written by /handover in an earlier session ({created}). That "
        "session's history is not in this context; this briefing stands in for it. The user's "
        "message below continues that work, so use the briefing instead of asking them to "
        "repeat themselves.]\n\n"
        f"{record['text']}"
    )

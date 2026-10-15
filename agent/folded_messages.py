"""Which messages a steer/redirect text stands for (fork: werkbank-chat, WB-1175).

A message that arrives while a turn runs is folded into that turn: ``AIAgent.steer()`` (its own
user row after the next tool batch, or the next turn when none follows) or ``AIAgent.redirect()``
(a user row after the cancelled model request). Several such messages can share ONE row, and the
model-facing text carries the gateway's origin preamble — so neither the row's ``content`` nor a
platform id column can say which messages it holds.

The gateway therefore hands the verbs a :class:`FoldedText`: the exact model-facing ``str`` plus
one part per folded message, ``{"ref": <id the user.message event announced>, "text": <the
sender's own words>}`` (an internal gateway event: ``{"ref": …, "internal": True}`` and no text).
The pending slots keep the parts while texts are joined, requeued or handed back, and the row the
text finally becomes carries them as ``display_metadata["folded_messages"]``. A surface (the
Werkbank chat) confirms announced messages by ``ref`` and shows each part's own text instead of
the row content. Every call contributes exactly one part — a plain ``str`` becomes
``{"ref": None, "text": …}`` once any part exists — so a surface that draws the parts never
loses a message. Pure data: no imports from the agent or the gateway.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

FOLDED_MESSAGES_KEY = "folded_messages"


class FoldedText(str):
    """A ``str`` (the model-facing text, unchanged) that knows the messages folded into it."""

    parts: tuple

    def __new__(cls, text: str, parts: Iterable[Dict[str, Any]] = ()):
        obj = super().__new__(cls, text)
        obj.parts = tuple(dict(p) for p in parts if isinstance(p, dict))
        return obj

    def __reduce__(self):  # copy/pickle keep the parts
        return (FoldedText, (str(self), [dict(p) for p in self.parts]))


def folded_parts(value: Any) -> List[Dict[str, Any]]:
    """The parts *value* carries (copies), ``[]`` for a plain ``str`` / ``None``."""
    parts = getattr(value, "parts", None) if isinstance(value, FoldedText) else None
    return [dict(p) for p in parts] if parts else []


def _part_for(value: Any, text: str) -> List[Dict[str, Any]]:
    parts = folded_parts(value)
    return parts if parts else ([{"ref": None, "text": text}] if text else [])


def join(existing: Optional[str], new: Any, cleaned: str, sep: str) -> str:
    """``existing + sep + cleaned`` (or *cleaned*), carrying the parts of both sides.

    *new* is the value the caller got (its parts count), *cleaned* the stripped text it stores.
    Without any part on either side the result is the plain ``str`` it always was."""
    text = (existing + sep + cleaned) if existing else cleaned
    if not folded_parts(existing) and not folded_parts(new):
        return text
    parts = (_part_for(existing, str(existing)) if existing else []) + _part_for(new, cleaned)
    return FoldedText(text, parts)


def strip(value: Any) -> Any:
    """``value.strip()`` keeping the parts (``None``/empty stay as they are)."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    parts = folded_parts(value)
    return FoldedText(stripped, parts) if parts and stripped else stripped


def metadata(value: Any) -> Dict[str, Any]:
    """``{"folded_messages": parts}`` for a row built from *value*, ``{}`` without parts."""
    parts = folded_parts(value)
    return {FOLDED_MESSAGES_KEY: parts} if parts else {}


def with_metadata(row: Dict[str, Any], value: Any) -> Dict[str, Any]:
    """*row* with the parts of *value* merged into its ``display_metadata`` (in place)."""
    extra = metadata(value)
    if extra:
        meta = row.get("display_metadata")
        row["display_metadata"] = {**(meta if isinstance(meta, dict) else {}), **extra}
    return row

"""Gateway side of the secret intake (WB-1212, core in ``agent/secret_intake.py``).

``intake_inbound_event`` runs at the gateway's earliest common point — the top of
``BasePlatformAdapter.handle_message`` (every adapter and ``PluginContext.inject_message`` funnel
through it), with the runner's ``_handle_message`` as a backstop for callers that build an event
and skip the adapter. It rewrites ``event.text`` / ``event.reply_to_text`` in place, so everything
downstream — busy queues, the runner, ``session_events.user_message`` (Werkbank stream), the session
store, transcript, compression, memory and the model request — only ever holds the placeholder.

The suggestion card goes out through the adapter's existing button path (``send_choice_picker``:
Telegram inline keyboard with its own callback authorization); the tap is answered by
``secret_intake.apply_choice`` in a worker thread, never by the model. Adapters without buttons get
a text notice instead (no value, and the value expires after the TTL).

WB-1261: adapters whose picker reports the tapping user (``choice_picker_reports_user = True``,
Telegram) get a third button „Kein Geheimnis“. Only the sender of the message may use it (the card's
event names them); the original then reaches the agent as an internal follow-up turn in the same
session („Freigegeben: …“, queued behind a running turn like every internal event) — nothing is saved.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, List, Optional

from agent import secret_intake

logger = logging.getLogger(__name__)

_CARD_TASKS: set = set()   # strong refs: a fire-and-forget task may otherwise be collected mid-send


def intake_inbound_event(adapter: Any, event: Any, session_key: str, *, surface: Optional[str] = None) -> int:
    """Redact *event* in place; schedule the card for every not-yet-offered placeholder it names.
    Returns the number of placeholders offered. Never raises.

    Fail-open (documented): when the detector itself raises, the text passes UNCHANGED (logged as a
    warning) — the run_conversation backstop and the steer/redirect stage scan again; a broken
    detector must not swallow the user's message."""
    if event is None or getattr(event, "internal", False):
        return 0
    try:
        if not secret_intake.enabled():
            return 0
        where = surface or _surface_of(event)
        sender = _sender_of(event)
        for attr in ("text", "reply_to_text"):
            value = getattr(event, attr, None)
            if isinstance(value, str) and value:
                res = secret_intake.intake(value, session_key, surface=where,
                                           sender=sender if attr == "text" else "")
                if res.text is not value:
                    setattr(event, attr, res.text)
        entries = secret_intake.offer_candidates(session_key, getattr(event, "text", "") or "", sender=sender)
    except Exception:
        logger.warning("secret_intake: inbound scan failed", exc_info=True)
        return 0
    if entries and adapter is not None:
        _schedule_card(adapter, event, session_key, entries)
    return len(entries)


def redact_for_session(text: Any, session_key: str, *, surface: str) -> Any:
    """*text* with every secret replaced (same placeholders as the session's intake); no card.
    Fails open (text unchanged, logged) only when the detector itself breaks."""
    if not isinstance(text, str) or not text:
        return text
    try:
        return secret_intake.intake(text, session_key, surface=surface).text
    except Exception:
        logger.warning("secret_intake: scan failed (text passes unchanged)", exc_info=False)
        return text


def intake_prepared_text(adapter: Any, event: Any, session_key: str, text: Any) -> Any:
    """Intake for the agent-facing text the runner PREPARED after ``handle_message`` (STT transcript,
    document/media notes, image descriptions, reply context, @-expansions): redact, then offer the
    card for placeholders not offered yet. Internal events pass unchanged."""
    if event is not None and getattr(event, "internal", False):
        return text
    if not isinstance(text, str) or not text:
        return text
    try:
        if not secret_intake.enabled():
            return text
        sender = _sender_of(event)
        text = secret_intake.intake(text, session_key, surface="gateway-prepared", sender=sender).text
        entries = secret_intake.offer_candidates(session_key, text, sender=sender)
    except Exception:
        logger.warning("secret_intake: prepared-text scan failed (text passes unchanged)", exc_info=False)
        return text
    if entries and adapter is not None:
        _schedule_card(adapter, event, session_key, entries)
    return text


def redact_platform_event(event: Any, session_key: str) -> Any:
    """``gateway_platform_event`` envelopes (``message_edited``) carry user text to plugin hooks:
    a copy with ``payload.text`` redacted; the envelope itself is not mutated. No card (an edit is
    no new message; the value waits for ``hermes vault`` or expires)."""
    if not isinstance(event, dict):
        return event
    payload = event.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str) or not payload["text"]:
        return event
    new_text = redact_for_session(payload["text"], session_key, surface="edit")
    if new_text == payload["text"]:
        return event
    return {**event, "payload": {**payload, "text": new_text}}


def _sender_of(event: Any) -> str:
    """User id of the event's sender (the only one who may release its secrets); "" when unknown."""
    return str(getattr(getattr(event, "source", None), "user_id", None) or "")


def _surface_of(event: Any) -> str:
    platform = getattr(getattr(event, "source", None), "platform", None)
    return str(getattr(platform, "value", platform) or "gateway")


def _schedule_card(adapter: Any, event: Any, session_key: str, entries: List[Any]) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(send_card(adapter, event, session_key, entries))
    _CARD_TASKS.add(task)
    task.add_done_callback(_CARD_TASKS.discard)


async def _answer(chat_id: str, value: str) -> str:
    return await asyncio.to_thread(secret_intake.apply_choice, value)


class ToastOnly(str):
    """Picker answer that must not replace the card: the adapter shows it as a short notice (Telegram:
    ``query.answer`` toast) and keeps card, buttons and picker state — a stranger's „Kein Geheimnis“
    tap must not take the sender's card away (WB-1261 F8)."""
    toast_only = True


def _card_answer(adapter: Any, event: Any, session_key: str):
    """Card callback bound to this card's adapter, event and session. ``user_id`` is the tapping user
    as the adapter reports it; „Kein Geheimnis“ is refused in the core unless it is the sender — that
    refusal comes back as :class:`ToastOnly` (nothing decided, the card stays for the sender)."""
    async def answer(chat_id: str, value: str, user_id: Optional[str] = None) -> str:
        released: List[tuple] = []
        refused: List[str] = []
        text = await asyncio.to_thread(
            secret_intake.apply_choice, value, actor=user_id, scope=session_key,
            on_release=lambda placeholder, original: released.append((placeholder, original)),
            on_refused=refused.append)
        if refused and not released:
            return ToastOnly(text)
        if released and not await _inject_released(adapter, event, session_key, released):
            text += "\n⚠️ Die Freigabe konnte nicht in die Sitzung gelegt werden; bitte den Text noch einmal schicken."
        return text
    return answer


def released_turn_text(released: List[tuple]) -> str:
    """Follow-up turn for the agent: which placeholder stands for which original text."""
    rows = [f"{placeholder} = {original}" for placeholder, original in released]
    return ("Freigegeben: Der Nutzer hat bestätigt, dass das kein Geheimnis ist. "
            "Der Platzhalter in seiner Nachricht steht für diesen Originaltext:\n" + "\n".join(rows))


async def _inject_released(adapter: Any, event: Any, session_key: str, released: List[tuple]) -> bool:
    """Inject the released originals as an internal turn into the card's session (an internal event
    skips the intake; the core's exception list keeps the run_conversation backstop quiet too)."""
    try:
        from gateway.platforms.event import MessageEvent, MessageType
        from gateway.session_identity import replace_source
        from gateway import wake
        source = getattr(event, "source", None)
        if source is None:
            return False
        if getattr(source, "message_id", None):
            source = replace_source(source, message_id=None)
        synth = MessageEvent(text=released_turn_text(released), message_type=MessageType.TEXT, source=source,
                             internal=True, metadata={"gateway_session_key": session_key})
        await wake.admit_internal_event(adapter, synth)
        return True
    except Exception as exc:   # never the exception text: it may carry the payload
        logger.warning("secret_intake: release not injected (%s)", type(exc).__name__)
        return False


async def send_card(adapter: Any, event: Any, session_key: str, entries: List[Any]) -> bool:
    """Post the "Im Tresor speichern / Verwerfen" card; text notice when the adapter has no buttons."""
    source = getattr(event, "source", None)
    chat_id = str(getattr(source, "chat_id", "") or "")
    if not chat_id:
        return False
    thread_id = getattr(source, "thread_id", None)
    metadata = {"thread_id": str(thread_id)} if thread_id else None
    title = secret_intake.card_text(entries)
    picker = getattr(adapter, "send_choice_picker", None)
    if callable(picker):
        try:
            reports_user = getattr(adapter, "choice_picker_reports_user", False) is True
            result = await picker(chat_id, title, secret_intake.card_choices(entries, release=reports_user),
                                  session_key, _card_answer(adapter, event, session_key), metadata=metadata)
            if getattr(result, "success", True) is not False:
                return True
        except Exception:
            logger.info("secret_intake: card not sent, falling back to a text notice", exc_info=False)
    notice = (title + "\nDiese Plattform hat keine Knöpfe dafür: speichern geht mit `hermes vault add` "
              "(Art „secret“).")
    try:
        await adapter.send(chat_id, notice, metadata=metadata)
    except Exception:
        logger.info("secret_intake: notice not sent", exc_info=False)
        return False
    return True

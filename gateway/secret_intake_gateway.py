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
    Returns the number of placeholders offered. Never raises (fails open on the TEXT path only when
    the detector itself breaks — logged)."""
    if event is None or getattr(event, "internal", False):
        return 0
    try:
        if not secret_intake.enabled():
            return 0
        where = surface or _surface_of(event)
        for attr in ("text", "reply_to_text"):
            value = getattr(event, attr, None)
            if isinstance(value, str) and value:
                res = secret_intake.intake(value, session_key, surface=where)
                if res.text is not value:
                    setattr(event, attr, res.text)
        entries = secret_intake.offer_candidates(session_key, getattr(event, "text", "") or "")
    except Exception:
        logger.warning("secret_intake: inbound scan failed", exc_info=True)
        return 0
    if entries and adapter is not None:
        _schedule_card(adapter, event, session_key, entries)
    return len(entries)


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
            result = await picker(chat_id, title, secret_intake.card_choices(entries), session_key, _answer,
                                  metadata=metadata)
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

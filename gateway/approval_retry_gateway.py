"""Run a claimed approval re-run and deliver its result (WB-1301).

Platform-neutral half of the "🔁 Nochmal ausführen" button: the adapter claims the offer
(``tools.approval_retry.claim``), then hands it here. Delivery order:

1. the requesting **subagent** is still running → its result is steered into it
   (``delegate_tool_registry.steer_subagent``);
2. the requesting **main session** is still the live session for its key → an internal
   ``[IMPORTANT: …]`` turn is injected into it (same path as other self-injected turns);
3. otherwise (subagent finished, session reset/ended) → exit code and a shortened output go
   into the chat, the full output as a file.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
from typing import Any, Optional

from tools import approval_retry

logger = logging.getLogger(__name__)


def main_session_alive(adapter: Any, offer: approval_retry.RetryOffer) -> bool:
    """The offer's session id is still the live session for its key."""
    if not offer.session_id or offer.source is None:
        return False
    store = getattr(adapter, "_session_store", None)
    peek = getattr(store, "peek_session_id", None)
    if not callable(peek):
        return False
    try:
        return peek(offer.session_key) == offer.session_id
    except Exception:
        logger.debug("approval retry: session lookup failed", exc_info=True)
        return False


async def _inject(adapter: Any, offer: approval_retry.RetryOffer, text: str) -> bool:
    try:
        from gateway import wake
        from gateway.platforms.event import MessageEvent, MessageType
        from gateway.session_identity import replace_source
        source = offer.source
        if getattr(source, "message_id", None):
            source = replace_source(source, message_id=None)
        event = MessageEvent(text=text, message_type=MessageType.TEXT, source=source, internal=True,
                             metadata={"gateway_session_key": offer.session_key,
                                       "notification_category": "result"})
        await wake.admit_internal_event(adapter, event)
        return True
    except Exception as exc:
        logger.warning("approval retry: result not injected into the session (%s)", type(exc).__name__)
        return False


def _chat_redact(text: str) -> str:
    try:
        from gateway.run import _redact_gateway_user_facing_secrets
        return _redact_gateway_user_facing_secrets(text)
    except Exception:
        from agent.redact import redact_sensitive_text
        return redact_sensitive_text(text)


def chat_report(offer: approval_retry.RetryOffer, result: approval_retry.RetryResult) -> str:
    """Short chat message: command, exit code, output tail (redacted)."""
    preview = offer.target.preview
    if len(preview) > 300:
        preview = preview[:300] + "…"
    out = result.output or "(keine Ausgabe)"
    if len(out) > approval_retry.CHAT_OUTPUT_CHARS:
        out = f"…(gekürzt, volle Ausgabe als Datei)\n{out[-approval_retry.CHAT_OUTPUT_CHARS:]}"
    code = "?" if result.exit_code is None else result.exit_code
    return _chat_redact(
        f"🔁 Nochmal ausgeführt ({offer.tool}) — die Sitzung lief nicht mehr, deshalb kommt das Ergebnis hier.\n"
        f"```\n{preview}\n```\nExit-Code: {code}\n```\n{out}\n```")


def _metadata(offer: approval_retry.RetryOffer) -> Optional[dict]:
    thread_id = offer.card.get("thread_id")
    return {"thread_id": thread_id} if thread_id else None


async def _post_to_chat(adapter: Any, offer: approval_retry.RetryOffer, result: approval_retry.RetryResult) -> bool:
    chat_id = offer.card.get("chat_id")
    if not chat_id:
        return False
    metadata = _metadata(offer)
    try:
        await adapter.send(chat_id, chat_report(offer, result), metadata=metadata)
    except Exception:
        logger.warning("approval retry: chat report not sent", exc_info=True)
        return False
    if result.output:
        path = None
        try:
            fd, path = tempfile.mkstemp(prefix="nochmal-", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(_chat_redact(result.output))
            await adapter.send_document(chat_id, path, caption="Volle Ausgabe",
                                        file_name=f"nochmal-{offer.tool}.txt", metadata=metadata)
        except Exception:
            logger.info("approval retry: output file not sent", exc_info=True)
        finally:
            if path:
                with contextlib.suppress(OSError):
                    os.unlink(path)
    return True


async def run_and_deliver(adapter: Any, offer: approval_retry.RetryOffer):
    """Execute the claimed offer and deliver; returns ``(outcome, result)`` with outcome
    ``subagent`` | ``session`` | ``chat`` | ``lost``."""
    result = await asyncio.to_thread(approval_retry.execute, offer)
    notice = approval_retry.agent_notice(offer, result)
    outcome = "lost"
    if offer.subagent_id:
        if approval_retry.steer_subagent(offer.subagent_id, notice):
            outcome = "subagent"
    elif main_session_alive(adapter, offer) and await _inject(adapter, offer, notice):
        outcome = "session"
    if outcome == "lost" and await _post_to_chat(adapter, offer, result):
        outcome = "chat"
    approval_retry.finish(offer, outcome, result)
    logger.info("Approval retry %s ran (tool=%s, exit=%s) → %s",
                offer.token[:8], offer.tool, result.exit_code, outcome)
    return outcome, result


def outcome_line(outcome: str, result_exit: Optional[int] = None) -> str:
    code = "" if result_exit is None else f" (Exit-Code {result_exit})"
    return {
        "subagent": f"✅ Nochmal ausgeführt{code} — Ergebnis an den Subagenten geliefert.",
        "session": f"✅ Nochmal ausgeführt{code} — Ergebnis an die Sitzung geliefert.",
        "chat": f"✅ Nochmal ausgeführt{code} — Ergebnis steht unten im Chat.",
    }.get(outcome, f"⚠️ Nochmal ausgeführt{code}, aber das Ergebnis konnte nicht zugestellt werden.")

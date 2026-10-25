"""Re-run an approval that expired, from the expired card itself (WB-1301).

When a gateway approval times out the agent has already been told "BLOCKED — silence is not
consent". The owner can still change their mind: the expired card gets a "🔁 Nochmal ausführen"
button (and a text reply "Nochmal" works the same). One press is exactly one approval for exactly
the command that expired:

* **Capture** — the terminal / execute_code handlers wrap their call in :func:`retry_target`, a
  context variable holding the tool, the exact gate text, the call arguments and a snapshot of the
  calling context (session vars, task id → same terminal environment). Nothing is stored yet.
* **Offer** — only the timeout settle hook (``gateway/run_turn_runner_approval_settle``) calls
  :func:`offer_for_timeout`. It refuses when the gate text does not match the expired request,
  when the command hits an unconditional floor (hardline, user deny rule, sudo -S guard: never
  approvable, so never a button), and for gates without a target (plugin rules, writes). An active
  deny never reaches it.
* **Claim** — :func:`claim` is the single atomic transition ``open → claimed|dropped``: bound to
  the delivered card (platform, chat, thread, message), to the session owner's user id when the
  session has one, valid for :data:`RETRY_TTL_S`. A second press finds ``claimed`` and does nothing.
* **Execute** — :func:`execute` re-invokes the same tool inside a copy of the captured context with
  a one-shot grant. The grant is consumed in ``tools.approval._human_decision`` — AFTER the floors
  ran again — so the button opens nothing that a normal "once" approval could not have opened.

Offers live in process memory only: a gateway restart drops them (fail closed; the card then
answers "abgelaufen"). The store is bounded (:data:`MAX_OFFERS`).
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

RETRY_TTL_S = 24 * 3600
MAX_OFFERS = 64
CHAT_OUTPUT_CHARS = 3000
AGENT_OUTPUT_CHARS = 6000

# Text replies to an expired card that act like the button (normalised: lower case, no trailing
# punctuation). Anything else falls through to the agent as a normal message.
RETRY_WORDS = frozenset({
    "nochmal", "nochmal ausführen", "nochmal bitte", "bitte nochmal", "erneut", "wiederholen",
    "ja", "ja bitte", "retry", "again", "yes", "🔁",
})


def is_retry_word(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    norm = " ".join(text.strip().lower().rstrip("!.?").split())
    return norm in RETRY_WORDS


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


# --- capture ---------------------------------------------------------------------------------------

@dataclass
class RetryTarget:
    """One tool call that may be re-run: what the gate saw and how to call the tool again."""
    tool: str                       # "terminal" | "execute_code"
    gate_text: str                  # exact ``command`` the approval gate received
    preview: str                    # raw command / code (redacted before display)
    rerun: Callable[[], str]        # calls the tool again with the same arguments
    cwd: Optional[str] = None
    task_id: Optional[str] = None
    subagent_id: Optional[str] = None
    context: Optional[contextvars.Context] = None


_current_target: contextvars.ContextVar[Optional[RetryTarget]] = contextvars.ContextVar(
    "approval_retry_target", default=None)


@contextlib.contextmanager
def retry_target(tool: str, gate_text: str, rerun: Callable[[], str], *, preview: Optional[str] = None,
                 cwd: Optional[str] = None, task_id: Optional[str] = None) -> Iterator[None]:
    """Mark the enclosed tool call as re-runnable should its approval expire."""
    target = RetryTarget(tool=tool, gate_text=gate_text, preview=gate_text if preview is None else preview,
                         rerun=rerun, cwd=cwd, task_id=task_id)
    target.context = contextvars.copy_context()
    token = _current_target.set(target)
    try:
        yield
    finally:
        _current_target.reset(token)


def _current_subagent_id(task_id: Optional[str]) -> Optional[str]:
    """The live subagent this call runs in (children run their tools under ``task_id ==
    subagent_id``); None in the main agent."""
    try:
        from agent.delegation_context import is_delegated_child_context
        if not is_delegated_child_context():
            return None
        from tools import delegate_tool_registry as reg
        with reg._active_subagents_lock:
            if task_id and task_id in reg._active_subagents:
                return task_id
            from gateway.session_context import get_session_env
            sid = get_session_env("HERMES_SESSION_ID", "")
            for record_id, record in reg._active_subagents.items():
                if sid and getattr(record.get("agent"), "session_id", None) == sid:
                    return record_id
    except Exception:
        logger.debug("approval retry: subagent lookup failed", exc_info=True)
    return None


# --- offers ---------------------------------------------------------------------------------------

@dataclass
class RetryOffer:
    token: str
    session_key: str
    session_id: Optional[str]
    request_id: Optional[str]
    owner_user_id: Optional[str]
    source: Any
    target: RetryTarget
    digest: str
    created_at: float
    state: str = "open"                       # open | claimed | dropped
    card: Dict[str, Optional[str]] = field(default_factory=dict)  # platform, chat_id, thread_id, message_id
    outcome: Optional[str] = None             # where the result went: subagent | session | chat
    display: str = ""                         # redacted command as shown on the card

    @property
    def tool(self) -> str:
        return self.target.tool

    @property
    def subagent_id(self) -> Optional[str]:
        return self.target.subagent_id


_lock = threading.Lock()
_offers: Dict[str, RetryOffer] = {}


def _never_approvable(target: RetryTarget) -> bool:
    """Unconditional floors (hardline, runtime self-delete, sudo -S guess, approvals.deny): these
    are refused before any human is asked, so a button may never exist for them."""
    try:
        from tools import approval
        if target.tool == "terminal":
            return approval._floor_block(target.gate_text, sudo_guard=True) is not None
        return False
    except Exception:
        logger.debug("approval retry: floor check failed — no button", exc_info=True)
        return True


def offer_for_timeout(session_key: str, approval_data: dict, *, source: Any = None,
                      session_id: Optional[str] = None, now: Optional[float] = None) -> Optional[str]:
    """Create a re-run offer for the request that just timed out; returns its token or None.

    Must run on the agent thread that waited (the settle hook does): the tool's
    :func:`retry_target` is read from the current context."""
    target = _current_target.get()
    if target is None or not session_key:
        return None
    from agent.redact import redact_sensitive_text
    shown = approval_data.get("command")
    if shown not in (target.gate_text, redact_sensitive_text(target.gate_text)):
        # The expired request is not this tool call's gate (e.g. a plugin rule inside it).
        return None
    if _never_approvable(target):
        return None
    target.subagent_id = _current_subagent_id(target.task_id)
    created = time.time() if now is None else now
    token = secrets.token_hex(16)
    offer = RetryOffer(
        token=token, session_key=session_key, session_id=session_id,
        request_id=approval_data.get("request_id"),
        owner_user_id=(str(getattr(source, "user_id", "") or "") or None) if source is not None else None,
        source=source, target=target,
        digest=_sha("\0".join((target.tool, target.gate_text, target.cwd or "", target.task_id or "",
                              session_key))),
        created_at=created, display=str(shown or ""),
    )
    with _lock:
        _offers[token] = offer
        while len(_offers) > MAX_OFFERS:
            _offers.pop(next(iter(_offers)))
    logger.info("Approval retry offer %s for request %s (tool=%s, session=%s)",
                token[:8], offer.request_id, target.tool, session_key)
    return token


def get_offer(token: str) -> Optional[RetryOffer]:
    with _lock:
        return _offers.get(token)


def bind_card(token: str, *, platform: str, chat_id: Any, message_id: Any, thread_id: Any = None,
              display: Optional[str] = None) -> bool:
    """Bind the offer to the delivered card; only presses/replies on exactly this message count."""
    with _lock:
        offer = _offers.get(token)
        if offer is None:
            return False
        if display is not None:
            offer.display = display
        offer.card = {"platform": platform, "chat_id": str(chat_id), "message_id": str(message_id),
                      "thread_id": None if thread_id is None else str(thread_id)}
        return True


def discard(token: str) -> None:
    with _lock:
        _offers.pop(token, None)


def token_for_card(platform: str, chat_id: Any, message_id: Any) -> Optional[str]:
    """The offer token whose card is this message (text reply path)."""
    with _lock:
        for token, offer in _offers.items():
            card = offer.card
            if (card.get("platform") == platform and card.get("chat_id") == str(chat_id)
                    and card.get("message_id") == str(message_id)):
                return token
    return None


@dataclass
class Claim:
    reason: str                     # ok | dropped | used | expired | unknown | foreign | wrong_card
    offer: Optional[RetryOffer] = None


def claim(token: str, *, platform: str, chat_id: Any, message_id: Any, thread_id: Any = None,
          user_id: Any = None, drop: bool = False, now: Optional[float] = None) -> Claim:
    """The one atomic transition of an offer. ``ok`` (run it now) and ``dropped`` happen at most
    once per offer; every later press reads ``used``. A foreign sender or a press on another
    message leaves the offer open for the owner."""
    current = time.time() if now is None else now
    with _lock:
        offer = _offers.get(token)
        if offer is None:
            return Claim("unknown")
        card = offer.card
        if (not card or card.get("platform") != platform or card.get("chat_id") != str(chat_id)
                or card.get("message_id") != str(message_id)
                or card.get("thread_id") != (None if thread_id is None else str(thread_id))):
            return Claim("wrong_card", offer)
        if offer.owner_user_id and str(user_id or "") != offer.owner_user_id:
            return Claim("foreign", offer)
        if offer.state != "open":
            return Claim("used", offer)
        if current - offer.created_at >= RETRY_TTL_S:
            offer.state = "dropped"
            return Claim("expired", offer)
        offer.state = "dropped" if drop else "claimed"
        return Claim("dropped" if drop else "ok", offer)


# --- execution ------------------------------------------------------------------------------------

class _Grant:
    __slots__ = ("gate_sha", "used")

    def __init__(self, gate_text: str):
        self.gate_sha = _sha(gate_text)
        self.used = False


_grant: contextvars.ContextVar[Optional[_Grant]] = contextvars.ContextVar("approval_retry_grant", default=None)


def consume_grant(command: str) -> bool:
    """True exactly once inside :func:`execute` for the offer's own gate text. Called by the
    approval engine where a human would be asked — after every floor already ran."""
    grant = _grant.get()
    if grant is None or grant.used or _sha(command or "") != grant.gate_sha:
        return False
    grant.used = True
    return True


@dataclass
class RetryResult:
    raw: str
    output: str
    exit_code: Optional[int]
    error: Optional[str] = None


def _parse_result(raw: Any) -> RetryResult:
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return RetryResult(raw=text, output=text, exit_code=None)
    if not isinstance(data, dict):
        return RetryResult(raw=text, output=text, exit_code=None)
    output = data.get("output")
    if output is None:
        output = data.get("stdout") or ""
    error = data.get("error") or None
    code = data.get("exit_code", data.get("returncode"))
    if not isinstance(code, int) or isinstance(code, bool):
        status = data.get("status")
        code = 0 if status in ("success", "completed") else (1 if status in ("error", "failed", "blocked") else None)
    if error and not output:
        output = str(error)
    return RetryResult(raw=text, output=str(output), exit_code=code, error=str(error) if error else None)


def execute(offer: RetryOffer) -> RetryResult:
    """Re-run the tool for a claimed offer (blocking; call from a worker thread)."""
    if offer.state != "claimed":
        raise RuntimeError("approval retry offer is not claimed")
    ctx = (offer.target.context or contextvars.copy_context()).copy()
    try:
        raw = ctx.run(_run_with_grant, offer.target)
    except Exception as exc:  # the tool itself failed: report, never raise into the adapter
        logger.warning("approval retry run failed: %s", type(exc).__name__)
        return RetryResult(raw="", output=f"{type(exc).__name__}: {exc}", exit_code=None, error=type(exc).__name__)
    return _parse_result(raw)


def _run_with_grant(target: RetryTarget) -> str:
    # A prepared terminal batch from the original turn must not leak a stale decision into the re-run.
    with contextlib.suppress(Exception):
        from agent import terminal_approval_batch as tab
        tab._batch.set(None)
        tab._slot.set(None)
    token = _grant.set(_Grant(target.gate_text))
    try:
        return target.rerun()
    finally:
        _grant.reset(token)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"…({len(text) - limit} Zeichen vorn gekürzt)\n" + text[-limit:]


def agent_notice(offer: RetryOffer, result: RetryResult) -> str:
    """The message the requesting agent receives when the owner re-ran its expired request."""
    preview = offer.target.preview
    if len(preview) > 400:
        preview = preview[:400] + "…"
    return (
        "[IMPORTANT: Der Owner hat eine abgelaufene Freigabe nachträglich erteilt (🔁 Nochmal ausführen). "
        f"Dein {offer.tool}-Aufruf, der vorhin mit BLOCKED/timeout endete, lief jetzt genau einmal.\n"
        f"Werkzeug: {offer.tool}\nBefehl: {preview}\n"
        f"Exit-Code: {'?' if result.exit_code is None else result.exit_code}\n"
        f"Ergebnis:\n{_clip(result.output, AGENT_OUTPUT_CHARS)}]"
    )


def finish(offer: RetryOffer, outcome: str, result: Optional[RetryResult] = None) -> None:
    """Record where the result went and tell session-event subscribers."""
    offer.outcome = outcome
    try:
        from gateway import session_events
        if session_events.enabled():
            session_events.emit(offer.session_key, offer.session_id, None, "approval.retried",
                                offer.request_id, None, {
                                    "request_id": offer.request_id, "tool": offer.tool, "delivered_to": outcome,
                                    "exit_code": None if result is None else result.exit_code})
    except Exception:
        logger.debug("approval.retried event failed", exc_info=True)


def steer_subagent(subagent_id: str, text: str) -> bool:
    try:
        from tools.delegate_tool_registry import steer_subagent as _steer
        return bool(_steer(subagent_id, text))
    except Exception:
        logger.debug("approval retry: steer failed", exc_info=True)
        return False


def _reset_for_tests() -> None:
    with _lock:
        _offers.clear()

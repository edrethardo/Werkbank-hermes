"""Slash commands a plugin hands to the gateway for a session (fork: werkbank-chat, WB-1256).

The Werkbank board chat (plugin ``platforms/werkbank``) sends ``/status``, ``/model``, ``/new``, …
with ``PluginContext.inject_message(text, session_key=…, as_command=True)``. The gateway then
runs the text through EXACTLY the command path a Telegram message of the session's owner takes
(``BasePlatformAdapter.handle_message`` → ``GatewayRunner._handle_message``: authorization,
``allow_admin_from`` slash gate, busy policy, unknown-command notice) — with two differences,
both bound to the event the plugin injected, never to a platform message:

* the command's reply does not go to the session's chat: it becomes a ``command.reply`` session
  event (the board draws it as a system line), and the handler's return value is swallowed;
* an interactive prompt the command opens (``/model`` picker, ``/reasoning`` / ``/fast`` choice
  picker, the destructive-command confirmation) is not sent to the chat either: it becomes a
  ``choice.request`` event — a card with numbered options, bound to its session — answered by
  :func:`answer` (the plugin's ``POST /v1/s/{key}/choices/{choice_id} {index}``).

A command that the gateway turns into an agent turn (``/plan``, a skill command, …) stays a turn:
its reply goes out like any answer (and the stream shows the turn); :func:`mark_turn` notes that.

Trust: :func:`is_plugin_command` accepts only an INTERNAL event flagged by the injector itself
(``hermes_plugin_injection`` + ``hermes_plugin_command``) — adapters never set either, so a chat
message cannot claim the board's surface.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

COMMAND_FLAG = "hermes_plugin_command"
REPLY_CAP = 20_000          # characters of a command reply in the stream
REQUEST_CAP = 2_000         # characters of the command text echoed in command.reply
TITLE_CAP = 2_000
LABEL_CAP = 200
MAX_OPTIONS = 600           # a /model card lists at most this many models
CHOICE_TTL_S = 30 * 60      # an unanswered card expires (like the Telegram picker state, which is
ANSWER_TIMEOUT_S = 120.0    # replaced by the next one); the picked callback may take this long
# A confirmation card lives exactly as long as its ``tools.slash_confirm`` entry (see
# ``ChoiceSurface.send_slash_confirm``): a later pick would find nothing to run.

_WRAPPED_ATTR = "_hm_plugin_command_wrapped"
_TURN_ATTR = "_hm_plugin_command_turn"

_surface: contextvars.ContextVar[Optional["ChoiceSurface"]] = contextvars.ContextVar(
    "hermes_plugin_command_surface", default=None)


# ── which events ──────────────────────────────────────────────────────────────────────────────


def is_plugin_command(event: Any) -> bool:
    """A slash command a plugin sent for a session (see the module docstring)."""
    meta = getattr(event, "metadata", None) or {}
    return (bool(getattr(event, "internal", False)) and bool(getattr(event, "allow_gateway_control", False))
            and meta.get("hermes_plugin_injection") is True and meta.get(COMMAND_FLAG) is True)


def needs_wrapping(event: Any) -> bool:
    return is_plugin_command(event) and not getattr(event, _WRAPPED_ATTR, False)


def mark_turn(event: Any) -> None:
    """The gateway runs this event as an agent turn (a prompt-rewriting command): its reply is the
    turn's answer, delivered as usual; pickers are no longer the command's."""
    if not is_plugin_command(event):
        return
    try:
        setattr(event, _TURN_ATTR, True)
    except Exception:
        pass
    surface = _surface.get()
    if surface is not None:
        surface.closed = True


def picker_surface() -> Optional["ChoiceSurface"]:
    """The board surface of the command being handled right now, else ``None`` (the chat's adapter)."""
    surface = _surface.get()
    return None if surface is None or surface.closed else surface


# ── events ────────────────────────────────────────────────────────────────────────────────────


def _meta(event: Any) -> Dict[str, Any]:
    return getattr(event, "metadata", None) or {}


def _platform(event: Any) -> Optional[str]:
    platform = getattr(getattr(event, "source", None), "platform", None)
    return getattr(platform, "value", None) or (str(platform) if platform else None)


def _command_name(event: Any) -> str:
    try:
        return str(event.get_command() or "")
    except Exception:
        return ""


def _emit(session_key: str, session_id: Optional[str], platform: Optional[str], type_: str,
          id_: Optional[str], payload: Dict[str, Any], plugin_id: Optional[str]) -> None:
    from gateway import session_events
    session_events.emit(session_key, session_id, platform, type_, id_, None, payload, origin=plugin_id)


def emit_reply(event: Any, text: str, *, session_id: Optional[str] = None, refused: bool = False) -> None:
    """``command.reply`` for *event*: ``{command, text, client_message_id, refused?}`` (id = the
    sender's id). ``text`` may be empty (a card was opened instead)."""
    meta = _meta(event)
    key = meta.get("gateway_session_key")
    if not key:
        return
    from gateway.session_events import _redact
    body = _redact(str(text or ""))[:REPLY_CAP]
    payload: Dict[str, Any] = {"command": _command_name(event), "text": body,
                               # the command as sent: a board whose POST has not returned yet draws
                               # the sender's bubble from it (the stream can be faster)
                               "request": _redact(str(getattr(event, "text", "") or ""))[:REQUEST_CAP],
                               "client_message_id": meta.get("client_message_id")}
    if refused:
        payload["refused"] = True
    _emit(str(key), session_id or meta.get("gateway_session_id"), _platform(event), "command.reply",
          meta.get("client_message_id"), payload, meta.get("hermes_plugin_id"))


async def _current_session_id(runner: Any, session_key: str) -> Optional[str]:
    """The session id behind *session_key* NOW (``/new`` replaced it): the reply belongs there."""
    store = getattr(runner, "async_session_store", None) or getattr(runner, "_async_session_store", None)
    lookup = getattr(store, "lookup_by_session_key", None)
    if lookup is None:
        return None
    try:
        entry = await lookup(session_key)
    except Exception:
        return None
    sid = getattr(entry, "session_id", None)
    return str(sid) if sid else None


async def handle(runner: Any, event: Any, inner: Callable[[Any], Awaitable[Any]]) -> Any:
    """Run *event* through *inner* (``GatewayRunner._handle_message``) with the board surface bound;
    publish the reply as ``command.reply`` and return ``None`` (nothing goes to the chat) — unless
    the command became an agent turn, whose answer is returned for normal delivery."""
    try:
        setattr(event, _WRAPPED_ATTR, True)
    except Exception:
        pass
    meta = _meta(event)
    surface = ChoiceSurface(str(meta.get("gateway_session_key") or ""), meta.get("hermes_plugin_id"),
                            meta.get("client_message_id"), _platform(event))
    # the command as sent rides with its cards like with command.reply (the stream can be faster than
    # the board's POST: the board draws the sender's bubble above the card from it)
    from gateway.session_events import _redact
    surface.request = _redact(str(getattr(event, "text", "") or ""))[:REQUEST_CAP]
    token = _surface.set(surface)
    try:
        result = await inner(event)
    except Exception as exc:
        logger.warning("Plugin command /%s failed: %s", _command_name(event), type(exc).__name__)
        surface.closed = True
        emit_reply(event, f"Befehl /{_command_name(event)} fehlgeschlagen ({type(exc).__name__}).",
                   session_id=await _current_session_id(runner, surface.session_key))
        return None
    finally:
        _surface.reset(token)
    if getattr(event, _TURN_ATTR, False):
        return result
    surface.closed = True
    emit_reply(event, "" if result is None else str(result),
               session_id=await _current_session_id(runner, surface.session_key))
    return None


# ── choice cards ──────────────────────────────────────────────────────────────────────────────


@dataclass
class _Choice:
    choice_id: str
    session_key: str
    kind: str
    title: str
    options: List[Dict[str, Any]]
    run: Callable[[int], Awaitable[Any]]
    loop: asyncio.AbstractEventLoop
    plugin_id: Optional[str]
    platform: Optional[str]
    client_message_id: Optional[str]
    request: str = ""
    ttl: float = CHOICE_TTL_S
    created: float = field(default_factory=time.monotonic)

    def payload(self) -> Dict[str, Any]:
        return {"choice_id": self.choice_id, "kind": self.kind, "title": self.title,
                "options": [dict(o) for o in self.options], "client_message_id": self.client_message_id,
                "request": self.request}


_lock = threading.Lock()
_open: Dict[str, _Choice] = {}


def _clip(value: Any, cap: int) -> str:
    return str(value if value is not None else "")[:cap]


class ChoiceExpired(Exception):
    """Raised by a card's callback when what it would answer is gone (a timed-out confirmation)."""


def _expired(choice: _Choice, now: Optional[float] = None) -> bool:
    return ((time.monotonic() if now is None else now) - choice.created) > choice.ttl


def _emit_closed(choice: _Choice, type_: str, extra: Optional[Dict[str, Any]] = None) -> None:
    _emit(choice.session_key, None, choice.platform, type_, choice.choice_id,
          {"choice_id": choice.choice_id, **(extra or {})}, choice.plugin_id)


class ChoiceSurface:
    """The adapter-shaped picker sink of ONE plugin command (``send_choice_picker``,
    ``send_model_picker``, ``send_slash_confirm`` — the methods the gateway looks for on the
    adapter TYPE). Every card it opens is bound to its session key."""

    def __init__(self, session_key: str, plugin_id: Optional[str], client_message_id: Optional[str],
                 platform: Optional[str]) -> None:
        self.session_key = session_key
        self.plugin_id = plugin_id
        self.client_message_id = client_message_id
        self.request = ""
        self.platform = platform
        self.closed = False

    def _open(self, kind: str, title: str, options: List[Dict[str, Any]],
              run: Callable[[int], Awaitable[Any]], ttl: float = CHOICE_TTL_S):
        from gateway.platforms.base import SendResult
        if not self.session_key or not options:
            return SendResult(success=False, error="no choices")
        choice = _Choice("ch_" + secrets.token_hex(8), self.session_key, kind, _clip(title, TITLE_CAP),
                         options[:MAX_OPTIONS], run, asyncio.get_running_loop(), self.plugin_id,
                         self.platform, self.client_message_id, self.request, ttl)
        with _lock:
            # One open card per session, like Telegram's one picker per chat: the new one wins.
            replaced = [c for c in _open.values() if c.session_key == self.session_key]
            for old in replaced:
                _open.pop(old.choice_id, None)
            _open[choice.choice_id] = choice
        for old in replaced:
            _emit_closed(old, "choice.expired", {"cause": "ersetzt"})
        _emit(self.session_key, None, self.platform, "choice.request", choice.choice_id, choice.payload(),
              self.plugin_id)
        return SendResult(success=True, message_id=choice.choice_id)

    async def send_choice_picker(self, chat_id: str, title: str, choices: list, session_key: str,
                                 on_choice_selected, metadata: Optional[Dict[str, Any]] = None):
        values = [str((c or {}).get("value") or "") for c in choices or []]
        options = [{"label": _clip((c or {}).get("label") or (c or {}).get("value"), LABEL_CAP),
                    "is_current": bool((c or {}).get("is_current"))} for c in choices or []]

        async def run(index: int):
            return await on_choice_selected(chat_id, values[index])
        return self._open("choice", title, options, run)

    async def send_model_picker(self, chat_id: str, providers: list, current_model: str, current_provider: str,
                                session_key: str, on_model_selected, metadata: Optional[Dict[str, Any]] = None):
        options: List[Dict[str, Any]] = []
        targets: List[Tuple[str, str]] = []
        for provider in providers or []:
            if not isinstance(provider, dict):
                continue
            slug = str(provider.get("slug") or "")
            group = _clip(provider.get("name") or slug, LABEL_CAP)
            for model in provider.get("models") or []:
                model_id = str(model.get("id") if isinstance(model, dict) else model or "")
                if not model_id or len(options) >= MAX_OPTIONS:
                    continue
                options.append({"label": _clip(model_id, LABEL_CAP), "group": group,
                                "is_current": model_id == current_model and slug == current_provider})
                targets.append((model_id, slug))
        title = f"Modell wählen — aktuell: {current_model or '?'} ({current_provider or '?'})"

        async def run(index: int):
            model_id, slug = targets[index]
            return await on_model_selected(chat_id, model_id, slug)
        return self._open("model", title, options, run)

    async def send_slash_confirm(self, chat_id: str, title: str, message: str, session_key: str,
                                 confirm_id: str, metadata: Optional[Dict[str, Any]] = None):
        picks = ("once", "always", "cancel")
        options = [{"label": label, "is_current": False}
                   for label in ("Ja, einmal", "Ja, nicht mehr fragen", "Abbrechen")]

        from tools import slash_confirm

        async def run(index: int):
            result = await slash_confirm.resolve(session_key, confirm_id, picks[index])
            if result is None:  # stale, superseded or already answered: nothing ran
                raise ChoiceExpired()
            return result
        # the card dies with the confirmation behind it, never later (a late pick would report
        # "done" while slash_confirm had already dropped the command)
        return self._open("confirm", f"{title}\n\n{message}", options, run,
                          ttl=slash_confirm.DEFAULT_TIMEOUT_SECONDS)


def pending(session_key: str) -> List[Dict[str, Any]]:
    """Open cards of *session_key* (the snapshot's ``pending_choices``); expired ones close here."""
    now = time.monotonic()
    with _lock:
        gone = [c for c in _open.values() if c.session_key == session_key and _expired(c, now)]
        for c in gone:
            _open.pop(c.choice_id, None)
        live = [c.payload() for c in _open.values() if c.session_key == session_key]
    for c in gone:
        _emit_closed(c, "choice.expired", {"cause": "abgelaufen"})
    return live


def answer(session_key: str, choice_id: str, index: Any, *, decided_by: str = "werkbank",
           timeout: float = ANSWER_TIMEOUT_S) -> Tuple[int, Dict[str, Any]]:
    """Pick option *index* of card *choice_id* — only a card of *session_key* (else 404, the card
    stays open for its own session). Called from a plugin thread, never on the gateway loop.
    Returns ``(status, payload)``; the card is closed exactly once (``choice.resolved``)."""
    if isinstance(index, bool) or not isinstance(index, int):
        return 400, {"error": "bad index", "grund": "Bitte eine Option wählen."}
    with _lock:
        choice = _open.get(choice_id)
        if choice is None or choice.session_key != session_key:
            return 404, {"error": "unknown choice", "grund": "Diese Auswahl ist nicht (mehr) offen."}
        if _expired(choice):
            _open.pop(choice_id, None)
            stale = choice
        else:
            stale = None
            if not 0 <= index < len(choice.options):
                return 400, {"error": "bad index", "grund": "Diese Option gibt es nicht."}
            _open.pop(choice_id, None)
    if stale is not None:
        _emit_closed(stale, "choice.expired", {"cause": "abgelaufen"})
        return 404, {"error": "unknown choice", "grund": "Diese Auswahl ist abgelaufen."}
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is choice.loop:
        logger.warning("plugin_commands.answer called on the gateway loop; refused")
        return 500, {"error": "internal", "grund": "Auswahl im falschen Thread."}
    label = choice.options[index].get("label", "")
    try:
        text = asyncio.run_coroutine_threadsafe(choice.run(index), choice.loop).result(timeout=timeout)
        text = "" if text is None else str(text)
        failed = False
    except ChoiceExpired:
        _emit_closed(choice, "choice.expired", {"cause": "abgelaufen"})
        return 404, {"error": "unknown choice", "grund": "Diese Auswahl ist abgelaufen."}
    except Exception as exc:
        logger.warning("Plugin choice %s failed: %s", choice.kind, type(exc).__name__)
        text, failed = f"Auswahl fehlgeschlagen ({type(exc).__name__}).", True
    from gateway.session_events import _redact
    text = _redact(text)[:REPLY_CAP]
    _emit_closed(choice, "choice.resolved", {"index": index, "label": label, "text": text,
                                             "decided_by": decided_by, **({"failed": True} if failed else {})})
    return 200, {"resolved": True, "index": index, "label": label, "text": text}


def _reset_for_tests() -> None:
    with _lock:
        _open.clear()

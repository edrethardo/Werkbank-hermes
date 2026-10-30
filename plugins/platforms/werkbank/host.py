"""The gateway as the plugin server sees it: one narrow object around the plugin context and the
live gateway runner (the owner of the published plugin message injector).

Everything that touches gateway internals sits here, guarded: an attribute that is missing in
this Hermes build degrades to "not available", never to an exception in a request.

Feature detection against WB-1127 (built in parallel):

* ``PluginContext.inject_message(..., client_message_id=…)`` — present: the id rides in the
  message's metadata and comes back on ``user.message``. Missing: the message is sent without it
  and the buffer gives the id to the next ``user.message`` this plugin injected (FIFO).
* ``PluginContext.interrupt_session(session_key)`` — missing: ``interrupt`` returns ``None`` and the
  server answers 501 with the reason.
* ``PluginContext.inject_message(..., reply_to_message_id=…)`` (WB-1148) — present: Hermes' answer
  replies to the mirrored message. Missing: the answer goes out as before, without a reply.
* ``PluginContext.inject_message(..., media_urls=…, media_types=…)`` (WB-1174) — present: board
  attachments reach the turn like a Telegram photo/document. Missing: the server answers 501 for a
  message with attachments; text goes on as before.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import mirror as mirror_mod
from . import resolve as resolve_mod

logger = logging.getLogger("plugins.platforms.werkbank")

TOOL_OUTPUT_MAX = 1024 * 1024        # GET tools/{id}/output: the full text, up to 1 MB
_TOOL_SCAN = 500                     # newest messages searched for a tool call id
MIRROR_WAIT_S = 5.0                  # the board's POST waits this long for the mirror's message id
COMPRESS_WAIT_S = 300.0              # WB-1319: the board's compress waits this long for the core
_ANCHOR_RE = re.compile(r"[0-9]{1,20}")   # = the core's reply_to_message_id check (plugins_injection.py)
NEW_SESSION_WAIT_S = 15.0            # WB-1342: thread + session creation on the gateway loop


class NewSessionRefused(Exception):
    """WB-1342: ``new_session`` could not open the chat — ``status`` and a short German reason.
    ``extra``: what exists in spite of the refusal (the start turn refused AFTER thread and session
    were created: ``{angelegt, session_id, session_key, thread_id}``), so the board keeps the chat
    as a draft instead of an orphaned topic; ``{}`` when nothing was created."""

    def __init__(self, status: int, grund: str, extra: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(grund)
        self.status = status
        self.grund = grund
        self.extra = dict(extra or {})


def accepts_kwarg(func: Any, name: str) -> bool:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class GatewayHost:
    def __init__(self, ctx: Any, home: Path) -> None:
        self.home = Path(home)
        self.profile = resolve_mod.profile_of(self.home)
        self.rebind(ctx)

    def rebind(self, ctx: Any) -> None:
        """Serve from ``ctx`` from now on: the context that created this host may be unloaded while
        another context of the plugin (a reload, a second scope) still holds the server."""
        self.ctx = ctx
        self.supports_client_message_id = accepts_kwarg(getattr(ctx, "inject_message", None),
                                                        "client_message_id")
        self.supports_interrupt = callable(getattr(ctx, "interrupt_session", None))
        self.supports_reply_anchor = accepts_kwarg(getattr(ctx, "inject_message", None),
                                                   "reply_to_message_id")      # WB-1148
        self.supports_media = accepts_kwarg(getattr(ctx, "inject_message", None), "media_urls")   # WB-1174
        self.supports_commands = accepts_kwarg(getattr(ctx, "inject_message", None), "as_command")  # WB-1256

    # -- the live gateway -------------------------------------------------------------------------
    def owner(self) -> Any:
        slot = getattr(getattr(self.ctx, "_manager", None), "_gateway_message_injector", None)
        return slot[0] if slot else None

    def _entry(self, session_key: str) -> Any:
        store = getattr(self.owner(), "session_store", None)
        lookup = getattr(store, "lookup_by_session_key", None)
        return lookup(session_key) if lookup is not None else None

    # -- resolve ----------------------------------------------------------------------------------
    def resolve(self, session_id: str) -> Optional[Dict[str, Any]]:
        return resolve_mod.resolve(self.owner(), session_id, self.profile)

    def current_session_id(self, session_key: str) -> Optional[str]:
        return resolve_mod.current_session_id(self.owner(), session_key)

    # -- send / stop ----------------------------------------------------------------------------
    def inject(self, session_key: str, text: str, client_message_id: str,
               reply_to_message_id: Optional[str] = None,
               media: Optional[List[Tuple[str, str]]] = None, command: bool = False) -> Tuple[bool, bool]:
        """(accepted, the id travels with the message). ``reply_to_message_id``: the message Hermes'
        answer replies to, passed only when this core knows the parameter. ``media``: ``(path, type)``
        attachments (WB-1174); refused (False) when this core cannot take them. ``command``
        (WB-1256): the text is a slash command (``as_command=True``); refused (False) — never sent as
        text — when this core does not know the parameter."""
        kwargs: Dict[str, Any] = {}
        if command:
            if not self.supports_commands:
                return False, self.supports_client_message_id
            kwargs["as_command"] = True
        if media:
            if not self.supports_media:
                return False, self.supports_client_message_id
            kwargs["media_urls"] = [path for path, _ in media]
            kwargs["media_types"] = [mtype for _, mtype in media]
        if self.supports_client_message_id:
            kwargs["client_message_id"] = client_message_id
        if self.supports_reply_anchor and _ANCHOR_RE.fullmatch(reply_to_message_id or ""):
            kwargs["reply_to_message_id"] = reply_to_message_id     # the core refuses anything else
        ok = self.ctx.inject_message(text, session_key=session_key, **kwargs)
        return bool(ok), self.supports_client_message_id

    def interrupt(self, session_key: str) -> Optional[bool]:
        if not self.supports_interrupt:
            return None
        try:
            return bool(self.ctx.interrupt_session(session_key))
        except Exception:
            logger.warning("werkbank: interrupt_session failed", exc_info=False)
            return False

    def _chat(self, session_key: str):
        """(adapter, source, loop) of the session's own chat, or ``None``."""
        owner = self.owner()
        entry = self._entry(session_key)
        if owner is None or entry is None or getattr(entry, "origin", None) is None:
            return None
        restore = getattr(owner, "_restored_source", None)
        source = restore(entry) if callable(restore) else entry.origin
        deliver = getattr(owner, "_delivery_adapter_for", None)
        adapter = deliver(source) if callable(deliver) and source is not None else None
        loop = getattr(owner, "_gateway_loop", None)
        if adapter is None or loop is None or loop.is_closed():
            return None
        return adapter, source, loop

    def new_session(self, next_to: str, title: str, text: str) -> Dict[str, Any]:
        """WB-1342: a NEW chat next to the chat of *next_to* — a sibling thread (Telegram DM topic,
        Slack/Matrix/Discord thread; the same rules as ``/branch``), that thread's own session, and a
        first turn started with *text* through ``inject_message(origin=…)``. No client message id:
        the start instruction is the board's, not the owner's words, so it stays hidden.

        Returns ``{session_key, session_id, thread_id}``; raises ``NewSessionRefused`` (404 unknown
        *next_to*, 501 chat without threads, 502 thread not created, 503 gateway not taking it)."""
        chat = self._chat(next_to)
        if chat is None:
            raise NewSessionRefused(404, "Die Vorlage-Sitzung hat keinen erreichbaren Chat.")
        if not self.injection_possible():
            raise NewSessionRefused(503, "Das Gateway nimmt gerade keine Nachrichten an.")
        adapter, source, loop = chat
        try:
            from gateway.slash_commands_branch_thread import branch_dest_source, branch_thread_parent
        except Exception:
            raise NewSessionRefused(501, "Dieses Hermes kann keine Nebenthemen anlegen.") from None
        parent = branch_thread_parent(source)
        create = getattr(adapter, "create_handoff_thread", None)
        if not parent or not callable(create):
            raise NewSessionRefused(501, "Dieser Chat kann kein neues Thema neben sich anlegen.")
        owner = self.owner()
        store = getattr(owner, "async_session_store", None)
        if store is None:
            raise NewSessionRefused(501, "Dieses Hermes hat keinen Sitzungsspeicher für ein neues Thema.")

        async def _open():
            thread_id = await create(parent, title)
            if not thread_id:
                return None
            dest = branch_dest_source(source, parent_id=parent, thread_id=str(thread_id), title=title)
            entry = await store.get_or_create_session(dest)
            return dest, entry, str(thread_id)

        try:
            opened = asyncio.run_coroutine_threadsafe(_open(), loop).result(timeout=NEW_SESSION_WAIT_S)
        except Exception:
            logger.warning("werkbank: new chat thread not opened", exc_info=False)
            opened = None
        if opened is None:
            raise NewSessionRefused(502, "Der Chat hat kein neues Thema angelegt.")
        dest, entry, thread_id = opened
        session_id = str(getattr(entry, "session_id", "") or "")
        session_key = str(getattr(entry, "session_key", "") or "")
        if not session_id or not session_key:
            raise NewSessionRefused(502, "Das neue Thema hat keine Sitzung bekommen.")
        try:
            ok = bool(self.ctx.inject_message(text, origin=dest.to_dict()))
        except Exception:
            ok = False
        if not ok:
            raise NewSessionRefused(503, f"Thema und Sitzung {session_id} sind angelegt, aber Hermes hat "
                                         "die Startanweisung nicht angenommen.",
                                    extra={"angelegt": True, "session_id": session_id,
                                           "session_key": session_key, "thread_id": thread_id})
        logger.info("werkbank: new chat opened from the board (thread created, start turn injected)")
        return {"session_key": session_key, "session_id": session_id, "thread_id": thread_id}

    def injection_possible(self) -> bool:
        """Can the gateway take a plugin message right now? Checked BEFORE the mirror goes out, so a
        message the gateway would refuse anyway does not push a notification and then vanish: a live
        injector, the gateway running and not draining, ``allow_gateway_injection`` granted. Only a
        pre-check — the state can change before ``inject`` (drain starts, config edited) and the core
        also checks the session's authorization; ``PostedMirror.take_back`` covers that window."""
        owner = self.owner()
        if owner is None or not getattr(owner, "_running", False) or getattr(owner, "_draining", False):
            return False
        allowed = getattr(self.ctx, "_gateway_injection_allowed", None)   # core-internal; absent: let inject decide
        try:
            return bool(allowed()) if callable(allowed) else True
        except Exception:
            return False

    def mirror(self, session_key: str, text: str, name: str = "") -> Optional["PostedMirror"]:
        """Post the board *text* into the session's own chat as the owner's message (``mirror.py``).

        Returns ``None`` when nothing was sent (no chat, injection impossible, send failed in time),
        else a ``PostedMirror``: ``message_id`` is the reply anchor for Hermes' answer when the send
        finished within ``MIRROR_WAIT_S``; a slower send still finishes in the background, and
        ``take_back()`` deletes it once it lands."""
        chat = self._chat(session_key)
        if chat is None or not self.injection_possible():
            return None
        adapter, source, loop = chat
        platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", None))
        who = mirror_mod.display_name(name)
        content, extra = mirror_mod.for_platform(str(platform or ""), text, who)
        thread_id = getattr(source, "thread_id", None)
        metadata = {**({"thread_id": str(thread_id)} if thread_id else {}), **extra} or None
        try:
            future = asyncio.run_coroutine_threadsafe(
                adapter.send(str(source.chat_id), content, metadata=metadata), loop)
        except Exception:
            logger.info("werkbank: mirror line not scheduled")
            return None
        posted = PostedMirror(self, session_key)
        try:
            result = future.result(timeout=MIRROR_WAIT_S)
        except Exception:                # timeout, cancelled, or the send raised
            future.add_done_callback(posted._landed_late)
            logger.info("werkbank: mirror line not delivered in time (answer without reply)")
            return posted
        message_id = _message_id(result)
        if message_id is None:
            logger.info("werkbank: mirror line not delivered")
            return None
        posted.message_id = message_id
        posted._landed(message_id)
        return posted

    def mirror_photos(self, session_key: str, paths: List[str], anchor: Optional[str] = None) -> int:
        """WB-1174: post the attached images into the session's chat as photos, replying to the mirror
        (*anchor*), one after the other. Best effort, not awaited; returns how many were scheduled."""
        chat = self._chat(session_key)
        send = getattr(chat[0], "send_image_file", None) if chat else None
        if not callable(send) or not paths:
            return 0
        _adapter, source, loop = chat
        thread_id = getattr(source, "thread_id", None)
        metadata = {"thread_id": str(thread_id)} if thread_id else None

        async def _all():
            for path in paths:
                try:
                    await send(str(source.chat_id), path, caption=None, reply_to=anchor, metadata=metadata)
                except Exception:
                    logger.info("werkbank: mirror photo not delivered")

        try:
            asyncio.run_coroutine_threadsafe(_all(), loop)
        except Exception:
            logger.info("werkbank: mirror photos not scheduled")
            return 0
        return len(paths)

    def unmirror(self, session_key: str, message_id: str) -> bool:
        """Delete a posted mirror message. Best effort, not awaited."""
        chat = self._chat(session_key)
        delete = getattr(chat[0], "delete_message", None) if chat else None
        if not callable(delete):
            return False
        try:
            asyncio.run_coroutine_threadsafe(delete(str(chat[1].chat_id), str(message_id)), chat[2])
        except Exception:
            return False
        return True

    # -- state for the snapshot --------------------------------------------------------------------
    def is_running(self, session_key: str) -> bool:
        running = getattr(self.owner(), "_running_agents", None)
        try:
            return bool(running) and session_key in running
        except Exception:
            return False

    def session_state(self, session_key: str) -> Dict[str, Any]:
        return {}

    def _db(self, session_key: str) -> Any:
        owner = self.owner()
        store = getattr(owner, "session_store", None)
        for_key = getattr(store, "_db_for_key", None)
        db = None
        if callable(for_key):
            try:
                db = for_key(session_key)
            except Exception:
                db = None
        if db is None:
            db = getattr(owner, "_session_db", None)
        return getattr(db, "_db", db)

    def history(self, session_key: str, session_id: Optional[str], limit: int) -> List[Dict[str, Any]]:
        db = self._db(session_key)
        get = getattr(db, "get_messages", None)
        if not session_id or get is None:
            return []
        try:
            return list(get(session_id, latest=True, limit=limit))
        except Exception:
            logger.info("werkbank: history not readable for the snapshot")
            return []

    def row_watermark(self, session_key: str, session_id: Optional[str]) -> Optional[int]:
        """Highest SessionDB row id of *session_id* (rewound rows included), ``0`` for none.

        ``messages.id`` is AUTOINCREMENT: every row written later has a higher id, whatever the
        clock does. ``None`` when the store cannot be read."""
        db = self._db(session_key)
        if not session_id:
            return None
        highest = getattr(db, "max_message_row_id", None)     # SELECT MAX(id): no message bodies read
        if callable(highest):
            try:
                rid = highest(session_id)
            except Exception:
                return None
            return rid if isinstance(rid, int) and not isinstance(rid, bool) else None
        get = getattr(db, "get_messages", None)               # a store without it (older core)
        if get is None:
            return None
        try:
            rows = list(get(session_id, include_inactive=True, latest=True, limit=1))
        except Exception:
            return None
        rid = rows[-1].get("id") if rows and isinstance(rows[-1], dict) else 0
        return rid if isinstance(rid, int) and not isinstance(rid, bool) else None

    def lineage(self, session_key: str, session_id: Optional[str]) -> List[str]:
        """*session_id*'s compression lineage (``SessionDB.get_compression_lineage``, root → tip) —
        never across ``/new``, ``/branch`` or a fork; ``[session_id]`` when the store cannot tell."""
        db = self._db(session_key)
        walk = getattr(db, "get_compression_lineage", None)
        if not session_id or not callable(walk):
            return [session_id] if session_id else []
        try:
            return [str(s) for s in walk(session_id) if s] or [session_id]
        except Exception:
            return [session_id]

    def tool_output(self, session_key: str, session_id: Optional[str], tool_call_id: str) -> Optional[str]:
        for row in reversed(self.history(session_key, session_id, _TOOL_SCAN)):
            if row.get("role") == "tool" and row.get("tool_call_id") == tool_call_id:
                content = row.get("content")
                text = content if isinstance(content, str) else str(content or "")
                try:
                    from gateway.session_events import _redact
                    text = _redact(text[:TOOL_OUTPUT_MAX])
                except Exception:
                    text = text[:TOOL_OUTPUT_MAX]
                return text
        return None

    def pending_approvals(self, session_key: str) -> List[Dict[str, Any]]:
        try:
            from tools.approval import list_gateway_approvals
            from gateway.session_events import redact_command
        except Exception:
            return []
        return [approval_payload(data, redact_command) for data in list_gateway_approvals(session_key)]

    def pending_choices(self, session_key: str) -> List[Dict[str, Any]]:
        """WB-1256: the open choice cards of the session (``gateway/plugin_commands.py``)."""
        from .commands import pending_choices
        return pending_choices(session_key)

    def pending_clarify(self, session_key: str) -> List[Dict[str, Any]]:
        try:
            from tools.clarify_gateway import get_pending_for_session
        except Exception:
            return []
        entry = get_pending_for_session(session_key, include_choice_prompts=True)
        return [] if entry is None else [clarify_payload(entry)]

    # -- WB-1319: the context editor ----------------------------------------------------------
    def _agent(self, session_key: str) -> Any:
        """The cached agent of the session (its ``/compress``, cache tier). Never the pending sentinel."""
        fn = getattr(self.owner(), "_cached_agent_for", None)
        agent = None
        if callable(fn):
            try:
                agent = fn(session_key, lockless_fallback=True)
            except Exception:
                agent = None
        try:
            from gateway.run import _AGENT_PENDING_SENTINEL
            if agent is _AGENT_PENDING_SENTINEL:
                return None
        except Exception:
            pass
        return agent

    def context_messages(self, session_key: str, session_id: Optional[str]) -> List[Dict[str, Any]]:
        """The active transcript in the shape the next turn loads it — the SAME read ``/compress`` and
        the gateway's live replay use (``get_messages_as_conversation``), never a raw table read.

        ``include_row_ids`` gives every message the row id the editor's operations name; it is also
        the loader shape a rewrite wants (no CAS digest, so a re-insert never nulls a column the
        projection does not decode)."""
        db = self._db(session_key)
        read = getattr(db, "get_messages_as_conversation", None)
        if not session_id or not callable(read):
            return []
        try:
            return list(read(session_id, repair_alternation=True, include_row_ids=True))
        except Exception:
            logger.info("werkbank: context not readable")
            return []

    def context_system(self, session_key: str, session_id: Optional[str]) -> Tuple[str, Any]:
        """(the prompt the last turn sent, the live agent) — the session row's saved prompt, which is
        what the model saw; the agent only names its cache tiers."""
        prompt = ""
        db = self._db(session_key)
        get_session = getattr(db, "get_session", None)
        if session_id and callable(get_session):
            try:
                row = get_session(session_id)
            except Exception:
                row = None
            if isinstance(row, dict) and isinstance(row.get("system_prompt"), str):
                prompt = row["system_prompt"]
        agent = self._agent(session_key)
        if not prompt and agent is not None:
            cached = getattr(agent, "_cached_system_prompt", "")
            prompt = cached if isinstance(cached, str) else ""
        return prompt, agent

    def context_write(self, session_key: str, session_id: Optional[str],
                      messages: List[Dict[str, Any]]) -> Tuple[bool, str]:
        """Replace the active transcript, rewind-style (the rows stay on disk as ``active = 0``), with
        the database's own turn-lease fence; then evict the cached agent so the next turn rebuilds
        its prompt from what is written."""
        db = self._db(session_key)
        write = getattr(db, "replace_messages", None)
        if not session_id or not callable(write):
            return False, "Diese Sitzung hat keinen schreibbaren Verlauf."
        try:
            write(session_id, messages, archive_dropped=True, reject_active_turn_lease=True)
        except Exception as exc:
            from hermes_state_errors import SessionTurnLeaseLostError
            if isinstance(exc, SessionTurnLeaseLostError):
                return False, "Ein Zug hält gerade den Verlauf — der Kontext ist schreibgeschützt."
            logger.info("werkbank: context write refused (%s)", type(exc).__name__)
            return False, "Die Datenbank hat die Änderung abgelehnt."
        evict = getattr(self.owner(), "_evict_cached_agent", None)
        if callable(evict):
            try:
                evict(session_key)
            except Exception:
                logger.info("werkbank: cached agent not evicted after a context edit")
        return True, ""

    def foreign_writer(self, session_key: str) -> str:
        """A writer that is not this gateway, if the core reports one (else ""). The plugin itself
        never writes a turn; a live TUI/stdio owner of the same durable id would."""
        hook = getattr(self.owner(), "_foreign_writer", None)      # core hook, may be absent
        if not callable(hook):
            return ""
        try:
            found = hook(session_key)
        except Exception:
            return ""
        return found if isinstance(found, str) else ""

    def write_gate(self, session_key: str) -> Tuple[bool, str]:
        """(may this request change the context?, reason). Owner and idle, checked server side:
        the gateway is up, no turn of this session runs, no foreign writer holds it."""
        if self.owner() is None:
            return False, "Hermes-Gateway läuft nicht — der Kontext ist schreibgeschützt."
        if self.is_running(session_key):
            return False, "Ein Zug läuft gerade — der Kontext ist schreibgeschützt."
        foreign = self.foreign_writer(session_key)
        if foreign:
            return False, foreign
        return True, ""

    def compress(self, session_key: str, focus_topic: str, keep_last: Optional[int],
                 preview: bool) -> Optional[Dict[str, Any]]:
        """The core's manual compression for this session (``_compress_from_plugin``), on the gateway
        loop. ``None``: this core has no such hook, or the session has no chat to compress."""
        hook = getattr(self.owner(), "_compress_from_plugin", None)
        loop = getattr(self.owner(), "_gateway_loop", None)
        chat = self._chat(session_key)
        if not callable(hook) or loop is None or getattr(loop, "is_closed", lambda: False)() or chat is None:
            return None
        try:
            future = asyncio.run_coroutine_threadsafe(
                hook(source=chat[1], focus_topic=focus_topic, keep_last=keep_last,
                     preview=preview), loop)
            return future.result(timeout=COMPRESS_WAIT_S)
        except Exception:
            logger.info("werkbank: compression failed (%s)", "timeout or error")
            return None


def _message_id(result: Any) -> Optional[str]:
    """The sent message's id from an adapter ``SendResult``; ``None`` when it was not delivered."""
    if getattr(result, "success", True) is False:
        return None
    message_id = getattr(result, "message_id", None)
    return str(message_id) if message_id not in (None, "") else None


class PostedMirror:
    """One mirror send. ``message_id``: the id known within ``MIRROR_WAIT_S`` (the reply anchor), else
    ``None``. ``take_back()`` — the gateway refused the message — deletes the mirror now, or, when
    the send is still running, as soon as it lands: whichever of ``take_back`` and the send's done
    callback comes second does the delete (both flags under one lock), so it happens exactly once."""

    def __init__(self, host: "GatewayHost", session_key: str) -> None:
        self.message_id: Optional[str] = None
        self._host, self._key = host, session_key
        self._lock = threading.Lock()
        self._landed_id: Optional[str] = None
        self._withdrawn = False

    def _landed(self, message_id: Optional[str]) -> None:
        with self._lock:
            self._landed_id = message_id
            delete = self._withdrawn and message_id is not None
        if delete:
            self._host.unmirror(self._key, message_id)

    def _landed_late(self, fut) -> None:
        try:
            message_id = _message_id(fut.result())
        except Exception:
            message_id = None
        if message_id is None:
            logger.info("werkbank: mirror line not delivered")
        self._landed(message_id)

    def take_back(self) -> None:
        with self._lock:
            self._withdrawn = True
            message_id = self._landed_id
        if message_id is not None:
            self._host.unmirror(self._key, message_id)


def approval_payload(data: Dict[str, Any], redact) -> Dict[str, Any]:
    """An open approval in the ``approval.request`` event payload shape (command scrubbed alike)."""
    return {
        "request_id": data.get("request_id"), "command": redact(data.get("command")),
        "description": redact(data.get("description")),
        "pattern_key": data.get("pattern_key", ""), "pattern_keys": list(data.get("pattern_keys") or []),
        "allow_permanent": data.get("allow_permanent", True), "allow_session": data.get("allow_session", True),
        "smart_denied": bool(data.get("smart_denied", False)), "surface": data.get("surface"),
        "delegated_child": bool(data.get("delegated_child", False)),
        "subagent_id": data.get("subagent_id"),
        "child_session_id": data.get("child_session_id"),
    }


def clarify_payload(entry: Any) -> Dict[str, Any]:
    """An open clarify prompt in the ``clarify.request`` event payload shape."""
    return {"clarify_id": entry.clarify_id, "question": str(entry.question or ""),
            "choices": list(entry.choices or []), "multi_select": bool(entry.multi_select),
            "awaiting_text": bool(getattr(entry, "awaiting_text", False))}

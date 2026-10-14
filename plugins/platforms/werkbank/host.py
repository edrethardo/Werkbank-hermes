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
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import mirror as mirror_mod
from . import resolve as resolve_mod

logger = logging.getLogger("plugins.platforms.werkbank")

TOOL_OUTPUT_MAX = 1024 * 1024        # GET tools/{id}/output: the full text, up to 1 MB
_TOOL_SCAN = 500                     # newest messages searched for a tool call id
MIRROR_WAIT_S = 5.0                  # the board's POST waits this long for the mirror's message id


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
               reply_to_message_id: Optional[str] = None) -> Tuple[bool, bool]:
        """(accepted, the id travels with the message). ``reply_to_message_id``: the message Hermes'
        answer replies to, passed only when this core knows the parameter."""
        kwargs: Dict[str, Any] = {}
        if self.supports_client_message_id:
            kwargs["client_message_id"] = client_message_id
        if reply_to_message_id is not None and self.supports_reply_anchor:
            kwargs["reply_to_message_id"] = reply_to_message_id
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

    def mirror(self, session_key: str, text: str, name: str = "") -> Optional[str]:
        """Post the board *text* into the session's own chat as the owner's message (``mirror.py``).

        Returns the posted message's id (the reply anchor for Hermes' answer), or ``None`` when it
        was not posted within ``MIRROR_WAIT_S`` — the send then still finishes in the background."""
        chat = self._chat(session_key)
        if chat is None:
            return None
        adapter, source, loop = chat
        platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", None))
        who = mirror_mod.display_name(name, getattr(source, "user_name", None))
        content, extra = mirror_mod.for_platform(str(platform or ""), text, who)
        thread_id = getattr(source, "thread_id", None)
        metadata = {**({"thread_id": str(thread_id)} if thread_id else {}), **extra} or None
        try:
            future = asyncio.run_coroutine_threadsafe(
                adapter.send(str(source.chat_id), content, metadata=metadata), loop)
        except Exception:
            logger.info("werkbank: mirror line not scheduled")
            return None
        try:
            result = future.result(timeout=MIRROR_WAIT_S)
        except Exception:                # timeout, cancelled, or the send raised
            future.add_done_callback(_log_late_mirror)
            logger.info("werkbank: mirror line not delivered in time (answer without reply)")
            return None
        if getattr(result, "success", True) is False:
            logger.info("werkbank: mirror line not delivered")
            return None
        message_id = getattr(result, "message_id", None)
        return str(message_id) if message_id not in (None, "") else None

    def unmirror(self, session_key: str, message_id: str) -> bool:
        """Delete a mirror whose message the gateway then refused. Best effort, not awaited."""
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

    def pending_clarify(self, session_key: str) -> List[Dict[str, Any]]:
        try:
            from tools.clarify_gateway import get_pending_for_session
        except Exception:
            return []
        entry = get_pending_for_session(session_key, include_choice_prompts=True)
        return [] if entry is None else [clarify_payload(entry)]


def _log_late_mirror(fut) -> None:
    try:
        result = fut.result()
    except Exception:
        logger.info("werkbank: mirror line not delivered")
        return
    if getattr(result, "success", True) is False:
        logger.info("werkbank: mirror line not delivered")


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

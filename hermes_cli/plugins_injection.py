"""Host routing for ``PluginContext.inject_message``: classic CLI REPL, Ink TUI / desktop, messaging
gateway (existing ``session_key`` or a new session at ``origin``)."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Optional

logger = logging.getLogger("hermes_cli.plugins")


_CLIENT_MESSAGE_ID_MAX = 256  # fork: werkbank-chat


def _valid_client_message_id(value: Any) -> bool:  # fork: werkbank-chat
    """A sender-chosen id: a non-empty, single-line string of bounded length."""
    return (isinstance(value, str) and 0 < len(value) <= _CLIENT_MESSAGE_ID_MAX  # fork: werkbank-chat
            and value.isprintable())  # fork: werkbank-chat


def inject_plugin_message(
    ctx: Any, content: str, role: str, *, session_key: Optional[str],
    origin: Optional[Mapping[str, Any]], client_message_id: Optional[str] = None,  # fork: werkbank-chat
) -> bool:
    manager = ctx._manager
    msg = content if role == "user" else f"[{role}] {content}"
    cli = manager._cli_ref
    # An origin names a messaging chat; the local REPL is never that chat.
    if cli is not None and origin is None:
        queue_ = cli._interrupt_queue if getattr(cli, "_agent_running", False) else cli._pending_input
        queue_.put(msg)
        return True
    if session_key and origin is not None:
        logger.warning("inject_message: pass session_key or origin, not both")
        return False
    if not session_key and origin is None:
        logger.warning("inject_message: gateway mode requires an existing session_key or an origin")
        return False
    if origin is not None and not isinstance(origin, Mapping):
        logger.warning("inject_message: origin must be a mapping in the SessionSource.to_dict() shape")
        return False
    if client_message_id is not None and not _valid_client_message_id(client_message_id):  # fork: werkbank-chat
        logger.warning("inject_message: client_message_id must be a short single-line string")  # fork: werkbank-chat
        return False  # fork: werkbank-chat
    if not ctx._gateway_injection_allowed():
        logger.warning("inject_message: gateway injection denied for plugin %s; set "
                       "plugins.entries.%s.allow_gateway_injection: true to allow it",
                       ctx.plugin_id, ctx.plugin_id)
        return False
    # TUI/desktop host is a different slot. It accepts only when it owns this
    # session_key; a miss falls through so a co-resident messaging gateway
    # still receives its own keys. An exception fails closed — do not also
    # hand the same text to the gateway.
    if session_key and manager.has_tui_message_injector:
        try:
            if manager.inject_tui_message(session_key=session_key, content=msg, plugin_id=ctx.plugin_id,
                                          plugin_home=manager.home_path):  # fork: werkbank-chat
                return True
        except Exception:
            logger.warning("inject_message: TUI scheduling failed for plugin %s", ctx.plugin_id, exc_info=True)
            return False
    if not manager.has_gateway_message_injector:
        logger.warning("inject_message: no live gateway is available")
        return False
    # The manager's immutable home is the plugin's profile (never the ambient HERMES_HOME of whatever
    # thread called us): the gateway creates an origin session there or nowhere, and an existing
    # session_key must belong to it (same rule as interrupt_session).
    if origin is None:
        target: dict[str, Any] = {"session_key": session_key,
                                  "plugin_home": manager.home_path}  # fork: werkbank-chat
    else:
        target = {"origin": dict(origin), "plugin_home": manager.home_path}
    if client_message_id is not None:  # fork: werkbank-chat
        target["client_message_id"] = client_message_id  # fork: werkbank-chat
    try:
        return bool(manager.inject_gateway_message(**target, content=msg, plugin_id=ctx.plugin_id))
    except Exception:
        logger.warning("inject_message: gateway scheduling failed for plugin %s", ctx.plugin_id, exc_info=True)
        return False


def interrupt_plugin_session(ctx: Any, session_key: Any) -> bool:  # fork: werkbank-chat
    """Stop a gateway session's running turn for ``PluginContext.interrupt_session``.

    Gateway only (the REPL and the TUI have their own stop keys). Same consent as injection; the
    gateway checks that the key belongs to the plugin's own profile (``plugin_home``)."""
    if not isinstance(session_key, str) or not session_key.strip():  # fork: werkbank-chat
        logger.warning("interrupt_session: a gateway session_key is required")  # fork: werkbank-chat
        return False  # fork: werkbank-chat
    if not ctx._gateway_injection_allowed():  # fork: werkbank-chat
        logger.warning("interrupt_session: denied for plugin %s; set "  # fork: werkbank-chat
                       "plugins.entries.%s.allow_gateway_injection: true to allow it",  # fork: werkbank-chat
                       ctx.plugin_id, ctx.plugin_id)  # fork: werkbank-chat
        return False  # fork: werkbank-chat
    manager = ctx._manager  # fork: werkbank-chat
    if not manager.has_gateway_message_injector:  # fork: werkbank-chat
        logger.warning("interrupt_session: no live gateway is available")  # fork: werkbank-chat
        return False  # fork: werkbank-chat
    try:  # fork: werkbank-chat
        return bool(manager.interrupt_gateway_session(  # fork: werkbank-chat
            session_key=session_key, plugin_id=ctx.plugin_id, plugin_home=manager.home_path))  # fork: werkbank-chat
    except Exception:  # fork: werkbank-chat
        logger.warning("interrupt_session: gateway scheduling failed for plugin %s", ctx.plugin_id, exc_info=True)  # fork: werkbank-chat
        return False  # fork: werkbank-chat

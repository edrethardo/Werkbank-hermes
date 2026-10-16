"""Plugin-triggered gateway turns (``PluginContext.inject_message``): the process-wide injector this
runner publishes, the thread-safe scheduler, and the dispatch through the adapter message path.

Two targets: an existing session (``session_key``; route and session id pinned from the store) or a
messaging chat (``origin``) whose session is created — or continued — through the normal session path
in the calling plugin's OWN profile. The origin path never chooses a profile: the gateway's routing
for that chat through that profile's adapter must already land in the plugin's profile, so a human
follow-up in the same chat keys into the same session."""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import weakref
from pathlib import Path
from typing import Any, Mapping, Optional

from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource

logger = logging.getLogger("gateway.run")


def _client_message_id_meta(client_message_id: Optional[str]) -> dict:  # fork: werkbank-chat
    """Metadata entry for a sender-chosen message id (absent when none was given)."""
    return {"client_message_id": client_message_id} if client_message_id else {}  # fork: werkbank-chat


def injecting_plugin(event: Any) -> Optional[str]:  # fork: werkbank-chat (WB-1182)
    """The id of the plugin that injected *event* (``PluginContext.inject_message``), else ``None``.

    Only an internal event flagged by the injector itself qualifies — a human message never does."""
    meta = getattr(event, "metadata", None) or {}  # fork: werkbank-chat
    if not getattr(event, "internal", False) or meta.get("hermes_plugin_injection") is not True:  # fork: werkbank-chat
        return None  # fork: werkbank-chat
    plugin_id = meta.get("hermes_plugin_id")  # fork: werkbank-chat
    return plugin_id if isinstance(plugin_id, str) and plugin_id else None  # fork: werkbank-chat


def _with_media(event: MessageEvent, media_urls: Optional[list], media_types: Optional[list]) -> MessageEvent:  # fork: werkbank-chat
    """Plugin attachments (validated by ``plugins_injection``) on *event*: all images → a PHOTO event,
    else DOCUMENT; ``media_text_inlined`` False — the content is not in the text, the agent reads the
    cached file (the document note says so). Without media the text event stays as it is."""
    if media_urls:  # fork: werkbank-chat
        types = list(media_types or [])  # fork: werkbank-chat
        event.message_type = (MessageType.PHOTO if all(t.startswith("image/") for t in types)  # fork: werkbank-chat
                              else MessageType.DOCUMENT)  # fork: werkbank-chat
        event.media_urls, event.media_types = list(media_urls), types  # fork: werkbank-chat
        event.media_text_inlined = [False] * len(media_urls)  # fork: werkbank-chat
    return event  # fork: werkbank-chat


def client_message_metadata(event: Any) -> dict:  # fork: werkbank-chat
    """``{"client_message_id": …, "hermes_plugin_id": …}`` for a plugin-injected event, each key
    only when known; ``{}`` for anything else.

    Merged into ``persist_user_display_metadata`` so the turn's SessionDB user row names its
    message and the plugin that sent it exactly. Only internal plugin injections qualify: a human
    message never does, and ``message_id`` (reply anchor, ledger key) is never touched. An
    injection WITHOUT a client id (a Werkbank wake order) still names its plugin (WB-1182), so a
    surface can show it as a system line instead of the owner's words — or not at all."""
    meta = getattr(event, "metadata", None) or {}  # fork: werkbank-chat
    if not getattr(event, "internal", False) or meta.get("hermes_plugin_injection") is not True:  # fork: werkbank-chat
        return {}  # fork: werkbank-chat
    cmid = meta.get("client_message_id")  # fork: werkbank-chat
    plugin_id = injecting_plugin(event)  # fork: werkbank-chat
    return {**({"client_message_id": cmid} if isinstance(cmid, str) and cmid else {}),  # fork: werkbank-chat
            **({"hermes_plugin_id": plugin_id} if plugin_id else {})}  # fork: werkbank-chat


def _session_key_profile(session_key: str) -> Optional[str]:  # fork: werkbank-chat
    """Profile named by a gateway key's ``agent:<ns>`` slot, ``None`` for a non-gateway key."""
    parts = session_key.split(":")  # fork: werkbank-chat
    if len(parts) < 3 or parts[0] != "agent" or not parts[1]:  # fork: werkbank-chat
        return None  # fork: werkbank-chat
    from gateway.session import profile_from_session_key_namespace  # fork: werkbank-chat
    return profile_from_session_key_namespace(parts[1])  # fork: werkbank-chat


class GatewayPluginInjectionMixin:
    def _install_plugin_message_injector(self) -> None:
        """Publish this live gateway's plugin message scheduler process-wide."""
        from hermes_cli.plugins import publish_gateway_message_host

        publish_gateway_message_host(self, self._schedule_plugin_message_injection)

    def _clear_plugin_message_injector(self) -> None:
        """Remove this runner's scheduler without clobbering a newer owner."""
        from hermes_cli.plugins import clear_published_gateway_message_host

        clear_published_gateway_message_host(self)

    def _schedule_plugin_message_injection(
        self, *, content: str, plugin_id: str, session_key: Optional[str] = None,
        origin: Optional[Mapping[str, Any]] = None, plugin_home: Optional[Path] = None,
        client_message_id: Optional[str] = None,  # fork: werkbank-chat
        reply_to_message_id: Optional[str] = None,  # fork: werkbank-chat
        media_urls: Optional[list] = None, media_types: Optional[list] = None,  # fork: werkbank-chat
    ) -> bool:
        """Schedule a plugin-triggered turn on the live gateway loop (thread-safe).

        With ``origin`` the profile, origin and adapter checks run here, synchronously, so the plugin
        gets ``False`` for a chat it may not start; authorization is rechecked at dispatch."""
        loop = getattr(self, "_gateway_loop", None)
        if not getattr(self, "_running", False) or loop is None or loop.is_closed():
            return False

        if origin is not None:
            source = self._plugin_origin_source(origin, plugin_home=plugin_home, plugin_id=plugin_id)
            if source is None:
                return False
            target = f"origin={source.platform.value}/{source.chat_id}"
            coro = self._dispatch_plugin_origin_injection(source=source, content=content, plugin_id=plugin_id,
                                                          client_message_id=client_message_id,  # fork: werkbank-chat
                                                          reply_to_message_id=reply_to_message_id,  # fork: werkbank-chat
                                                          media_urls=media_urls, media_types=media_types)  # fork: werkbank-chat
        else:
            if not isinstance(session_key, str) or not self._plugin_session_in_own_profile(  # fork: werkbank-chat
                    session_key, plugin_home=plugin_home, plugin_id=plugin_id,  # fork: werkbank-chat
                    what="Plugin message injection"):  # fork: werkbank-chat
                return False  # fork: werkbank-chat
            target = f"session={session_key}"
            coro = self._dispatch_plugin_message_injection(
                session_key=session_key, content=content, plugin_id=plugin_id,
                client_message_id=client_message_id,  # fork: werkbank-chat
                reply_to_message_id=reply_to_message_id,  # fork: werkbank-chat
                media_urls=media_urls, media_types=media_types,  # fork: werkbank-chat
            )
        return self._submit_plugin_coroutine(coro, loop, plugin_id=plugin_id, target=target,  # fork: werkbank-chat
                                             what="Plugin message injection")  # fork: werkbank-chat

    def _submit_plugin_coroutine(  # fork: werkbank-chat
        self, coro, loop, *, plugin_id: str, target: str, what: str,
    ) -> bool:
        """Run *coro* on the gateway loop from any thread; log a falsy or failed result."""
        from gateway.run import safe_schedule_threadsafe
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop is loop:
            try:
                future = loop.create_task(coro)
            except Exception:
                coro.close()
                logger.warning("%s scheduling failed", what, exc_info=True)  # fork: werkbank-chat
                return False
            self._background_tasks.add(future)
            future.add_done_callback(self._background_tasks.discard)
        else:
            future = safe_schedule_threadsafe(
                coro, loop, logger=logger, log_message=f"{what} scheduling failed",  # fork: werkbank-chat
                log_level=logging.WARNING,
            )
            if future is None:
                return False

        def _log_result(completed) -> None:
            try:
                if completed.result():
                    return
            except (asyncio.CancelledError, concurrent.futures.CancelledError):
                return
            except Exception:  # dispatch boundary: the plugin already got True; log, never raise
                logger.warning("%s failed: plugin=%s %s", what, plugin_id, target, exc_info=True)  # fork: werkbank-chat
                return
            logger.warning("%s was not routed: plugin=%s %s", what, plugin_id, target)  # fork: werkbank-chat

        future.add_done_callback(_log_result)
        return True

    def _plugin_injection_accepting(self) -> bool:
        return getattr(self, "_running", False) and not getattr(self, "_draining", False)

    def _plugin_injection_scope(self, source: SessionSource):
        """Multiplex: enter the source's profile runtime scope around ``handle_message`` so the
        background task it spawns — the turn AND the reply send after the handler returns — inherits
        that profile's home and secrets (this coroutine runs from the default loop context, not from
        the profile adapter's own intake task)."""
        if getattr(getattr(self, "config", None), "multiplex_profiles", False) is not True:
            return contextlib.nullcontext()
        from gateway.run import _async_profile_runtime_scope
        return _async_profile_runtime_scope(self._resolve_profile_home_for_source(source))

    def _plugin_injection_authorized(self, source: SessionSource, *, plugin_id: str, target: str) -> bool:
        """Current gateway authorization for the route the injected turn will run as. Host-side
        plugin code is trusted to ask, never to widen access: a stored or supplied route must pass
        today's allowlists / pairing / allow-all, and adapter-time grants (Discord roles) do not count."""
        try:
            authorized = self._is_user_authorized_for_source(source, allow_adapter_delegation=False)
        except Exception:
            logger.warning("Plugin message injection authorization check failed: plugin=%s %s",
                           plugin_id, target, exc_info=True)
            return False
        if not authorized:
            logger.warning("Plugin message injection denied by current gateway authorization: plugin=%s %s",
                           plugin_id, target)
        return authorized

    async def _dispatch_plugin_message_injection(
        self, *, session_key: str, content: str, plugin_id: str,
        client_message_id: Optional[str] = None,  # fork: werkbank-chat
        reply_to_message_id: Optional[str] = None,  # fork: werkbank-chat
        media_urls: Optional[list] = None, media_types: Optional[list] = None,  # fork: werkbank-chat
    ) -> bool:
        """Route a plugin-triggered turn through the session's live adapter. ``reply_to_message_id``
        becomes the turn's reply anchor (``reply_anchor_override``); ``message_id`` stays unset."""
        if not self._plugin_injection_accepting():
            return False
        entry = await self.async_session_store.lookup_by_session_key(session_key)
        if entry is None or entry.origin is None or not self._plugin_injection_accepting():
            return False

        from gateway.session_identity import replace_source
        source = replace_source(self._restored_source(entry))
        if not self._plugin_injection_authorized(source, plugin_id=plugin_id, target=f"session={session_key}"):
            return False

        adapter = self._delivery_adapter_for(source)
        if adapter is None:
            return False

        async with self._plugin_injection_scope(source):
            await adapter.handle_message(_with_media(MessageEvent(  # fork: werkbank-chat
                text=content, message_type=MessageType.TEXT, source=source, internal=True,
                allow_gateway_control=False,
                reply_anchor_override=reply_to_message_id,  # fork: werkbank-chat
                metadata={
                    "hermes_plugin_id": plugin_id, "hermes_plugin_injection": True,
                    "gateway_session_key": session_key, "gateway_session_id": entry.session_id,
                    "gateway_session_strict": True,
                    **_client_message_id_meta(client_message_id),  # fork: werkbank-chat
                },
            ), media_urls, media_types))  # fork: werkbank-chat
        logger.info(
            "Plugin message injection dispatched: plugin=%s session=%s session_id=%s",
            plugin_id, session_key, entry.session_id,
        )
        return True

    def _served_profile_for_home(self, home: Optional[Path]) -> Optional[str]:
        """Name of the profile this gateway serves at *home* (a plugin manager's immutable home), or
        ``None`` when this gateway does not serve it."""
        if home is None:
            return None
        from hermes_constants import get_routing_process_hermes_home, hermes_home_key
        key = hermes_home_key(home)
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            from gateway.run import _multiplex_profile_homes
            try:
                served = _multiplex_profile_homes(self.config)
            except Exception:
                logger.warning("Plugin message injection: served-profile set unavailable", exc_info=True)
                return None
            return next((name for name, served_home in served if hermes_home_key(served_home) == key), None)
        if hermes_home_key(get_routing_process_hermes_home()) != key:
            return None
        return getattr(self, "_primary_profile_name", None) or self._active_profile_name()

    def _plugin_origin_source(
        self, origin: Mapping[str, Any], *, plugin_home: Optional[Path], plugin_id: str,
    ) -> Optional[SessionSource]:
        """The canonicalized source for a plugin-supplied *origin*, or ``None`` (logged) when the
        plugin may not start a turn there: its profile is not served, the origin is malformed or
        names another profile, that profile has no live adapter for the platform, or the gateway's
        own routing for the chat lands in a different profile."""
        def _refuse(reason: str, *args: Any) -> None:
            logger.warning("Plugin message injection refused: plugin=%s " + reason, plugin_id, *args)

        profile = self._served_profile_for_home(plugin_home)
        if profile is None:
            return _refuse("its profile home %s is not served by this gateway", plugin_home)
        fields = dict(origin)
        requested = fields.pop("profile", None)
        if requested and requested != profile:
            return _refuse("origin names profile %r; plugins may only start sessions in their own profile (%s)",
                           requested, profile)
        try:
            source = SessionSource.from_dict(fields)
        except (KeyError, TypeError, ValueError) as exc:
            return _refuse("invalid origin (%s)", exc)
        where = f"{source.platform.value}/{source.chat_id}"
        adapter = self._adapters_for_profile(profile).get(source.platform)
        if adapter is None:
            return _refuse("no connected %s adapter for profile %s", source.platform.value, profile)
        # Same provenance build_source() stamps on a live inbound event from this adapter, so the
        # identity (receiving bot, runtime profile, authorization home) resolves exactly as it would
        # for a human message in this chat.
        source._transport_adapter_ref = weakref.ref(adapter)
        identity = self._canonicalize(source)
        if identity is None:
            return _refuse("origin %s does not resolve to a served profile", where)
        if identity.runtime_profile != profile:
            return _refuse("origin %s routes to profile %s, not the plugin's profile %s",
                           where, identity.runtime_profile, profile)
        return source

    async def _dispatch_plugin_origin_injection(
        self, *, source: SessionSource, content: str, plugin_id: str,
        client_message_id: Optional[str] = None,  # fork: werkbank-chat
        reply_to_message_id: Optional[str] = None,  # fork: werkbank-chat
        media_urls: Optional[list] = None, media_types: Optional[list] = None,  # fork: werkbank-chat
    ) -> bool:
        """Start (or continue) the session at a plugin-supplied origin through the adapter's normal
        message path: ``get_or_create_session`` creates it, the reply goes back to that chat."""
        target = f"origin={source.platform.value}/{source.chat_id}"
        if not self._plugin_injection_accepting():
            return False
        adapter = self._delivery_adapter_for(source)
        if adapter is None:
            logger.warning("Plugin message injection: adapter for %s went away before dispatch", target)
            return False
        if not self._plugin_injection_authorized(source, plugin_id=plugin_id, target=target):
            return False
        session_key = self._session_key_for_source(source)
        async with self._plugin_injection_scope(source):
            await adapter.handle_message(_with_media(MessageEvent(  # fork: werkbank-chat
                text=content, message_type=MessageType.TEXT, source=source, internal=True,
                allow_gateway_control=False,
                reply_anchor_override=reply_to_message_id,  # fork: werkbank-chat
                # Non-strict: the key is pinned against topic recovery, the session may not exist yet.
                metadata={
                    "hermes_plugin_id": plugin_id, "hermes_plugin_injection": True,
                    "gateway_session_key": session_key,
                    **_client_message_id_meta(client_message_id),  # fork: werkbank-chat
                },
            ), media_urls, media_types))  # fork: werkbank-chat
        logger.info("Plugin message injection dispatched: plugin=%s %s session=%s", plugin_id, target, session_key)
        return True

    # ── plugin stop (PluginContext.interrupt_session) ─────────────────────────────  # fork: werkbank-chat

    def _plugin_session_in_own_profile(  # fork: werkbank-chat
        self, session_key: str, *, plugin_home: Optional[Path], plugin_id: str,
        what: str = "Plugin interrupt",  # fork: werkbank-chat
    ) -> bool:
        """True when *session_key* belongs to the profile the plugin's manager lives in.

        The plugin's home must be served by this gateway. Multiplexed, the key's ``agent:<ns>``
        namespace must name that profile; single-profile, every key of this gateway belongs to
        its one served home."""
        profile = self._served_profile_for_home(plugin_home)
        if profile is None:
            logger.warning("%s refused: plugin=%s profile home %s is not served by this gateway",  # fork: werkbank-chat
                           what, plugin_id, plugin_home)
            return False
        key_profile = _session_key_profile(session_key)
        if key_profile is None:
            logger.warning("%s refused: plugin=%s %r is not a gateway session key",  # fork: werkbank-chat
                           what, plugin_id, session_key)
            return False
        if getattr(getattr(self, "config", None), "multiplex_profiles", False) is True and key_profile != profile:
            logger.warning("%s refused: plugin=%s session %s belongs to profile %s, not %s",  # fork: werkbank-chat
                           what, plugin_id, session_key, key_profile, profile)
            return False
        return True

    def _schedule_plugin_session_interrupt(  # fork: werkbank-chat
        self, *, session_key: str, plugin_id: str, plugin_home: Optional[Path] = None,
    ) -> bool:
        """Schedule a ``/stop``-equivalent for *session_key* (thread-safe). ``False`` when refused
        (gateway down, foreign profile) or when no turn runs under that key."""
        loop = getattr(self, "_gateway_loop", None)
        if not getattr(self, "_running", False) or loop is None or loop.is_closed():
            return False
        if not isinstance(session_key, str) or not session_key:
            return False
        if not self._plugin_session_in_own_profile(session_key, plugin_home=plugin_home, plugin_id=plugin_id):
            return False
        if self._running_agents.get(session_key) is None:
            return False
        coro = self._dispatch_plugin_session_interrupt(session_key=session_key, plugin_id=plugin_id)
        return self._submit_plugin_coroutine(coro, loop, plugin_id=plugin_id, target=f"session={session_key}",
                                             what="Plugin interrupt")

    async def _dispatch_plugin_session_interrupt(self, *, session_key: str, plugin_id: str) -> bool:  # fork: werkbank-chat
        """Run the ``/stop`` interrupt for a plugin: same hard interrupt and cleanup, the
        ``agent_loop_stopped`` hook attributed to ``plugin:<plugin_id>``."""
        if not getattr(self, "_running", False):
            return False
        entry = await self.async_session_store.lookup_by_session_key(session_key)
        if entry is None or entry.origin is None:
            return False
        from gateway.session_identity import replace_source
        source = replace_source(self._restored_source(entry))
        if not self._plugin_injection_authorized(source, plugin_id=plugin_id, target=f"session={session_key}"):
            return False
        if self._running_agents.get(session_key) is None:
            return False
        from gateway.run import _INTERRUPT_REASON_STOP
        async with self._plugin_injection_scope(source):
            await self._interrupt_and_clear_session(
                session_key, source, interrupt_reason=_INTERRUPT_REASON_STOP,
                invalidation_reason="plugin_interrupt", hook_reason=f"plugin:{plugin_id}",
            )
        logger.info("Plugin interrupt: plugin=%s session=%s", plugin_id, session_key)
        return True

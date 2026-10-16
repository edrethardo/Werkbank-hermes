"""Tests for plugin-triggered turns in existing gateway sessions."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import hermes_yaml as yaml

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import (
    BasePlatformAdapter,
    PlatformConfig,
)
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource, SessionStore, build_session_key
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


def _entry(*, origin=True) -> SessionEntry:
    source = None
    if origin:
        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="42",
            chat_type="dm",
            user_id="42",
            user_name="tester",
        )
    now = datetime.now()
    return SessionEntry(
        session_key="agent:main:telegram:dm:42",
        session_id="session-42",
        created_at=now,
        updated_at=now,
        origin=source,
        platform=Platform.TELEGRAM,
    )


def _runner(entry: SessionEntry | None, adapter=None) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.session_store = SimpleNamespace()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store, lookup_by_session_key=AsyncMock(return_value=entry)
    )
    runner.adapters = {Platform.TELEGRAM: adapter} if adapter else {}
    runner._profile_adapters = {}
    runner._running = True
    runner._draining = False
    runner._background_tasks = set()
    runner._is_user_authorized = MagicMock(return_value=True)
    return runner


class _RoutingAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.TELEGRAM)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        raise AssertionError("network send is not expected")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "dm"}


@pytest.mark.asyncio
async def test_plugin_context_routes_through_live_gateway_to_existing_session(
    tmp_path,
    monkeypatch,
):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({
            "plugins": {"entries": {"notify-plugin": {"allow_gateway_injection": True}}}
        })
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    source = _entry().origin
    entry = store.get_or_create_session(source)
    adapter = _RoutingAdapter()
    adapter.set_message_handler(AsyncMock())
    adapter._active_sessions[entry.session_key] = asyncio.Event()
    pending_user_event = MessageEvent(
        text="human follow-up",
        message_type=MessageType.PHOTO,
        source=source,
        media_urls=["human.jpg"],
        media_types=["image/jpeg"],
    )
    adapter._pending_messages[entry.session_key] = pending_user_event

    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._profile_adapters = {}
    runner._gateway_loop = asyncio.get_running_loop()
    runner._running = True
    runner._draining = False
    runner._background_tasks = set()
    runner._queued_events = {}
    runner._is_user_authorized = MagicMock(return_value=True)
    adapter.set_busy_session_handler(runner._handle_active_session_busy_message)

    manager = PluginManager()
    context = PluginContext(
        PluginManifest(name="notify-plugin", key="notify-plugin", source="user"),
        manager,
    )

    # The runner publishes process-wide; expose this standalone manager through the legacy slot so
    # the real publisher stamps it while this integration test still exercises the scheduler.
    with patch("hermes_cli.plugins._plugin_manager", manager):
        runner._install_plugin_message_injector()
        assert (
            context.inject_message(
                "/approve always",
                session_key=entry.session_key,
            )
            is True
        )
        task = next(iter(runner._background_tasks))
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

        assert adapter._pending_messages[entry.session_key] is pending_user_event
        queued = runner._queued_events[entry.session_key][0]
        assert pending_user_event.text == "human follow-up"
        assert pending_user_event.media_urls == ["human.jpg"]
        assert pending_user_event.allow_gateway_control is True
        assert queued.text == "/approve always"
        assert queued.allow_gateway_control is False
        assert queued.metadata["gateway_session_id"] == entry.session_id
        adapter._message_handler.assert_not_awaited()

        runner._clear_plugin_message_injector()
        assert manager.has_gateway_message_injector is False


@pytest.mark.asyncio
async def test_dispatch_uses_stored_origin_and_adapter_message_path():
    adapter = SimpleNamespace(handle_message=AsyncMock())
    entry = _entry()
    runner = _runner(entry, adapter)

    accepted = await runner._dispatch_plugin_message_injection(
        session_key=entry.session_key,
        content="check the deployment",
        plugin_id="notify-plugin",
    )

    assert accepted is True
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "check the deployment"
    assert event.internal is True
    assert event.allow_gateway_control is False
    assert event.get_command() is None
    assert event.source == entry.origin
    assert event.source is not entry.origin
    runner._is_user_authorized.assert_called_once_with(
        event.source,
        allow_adapter_delegation=False,
    )
    assert event.metadata == {
        "hermes_plugin_id": "notify-plugin",
        "hermes_plugin_injection": True,
        "gateway_session_key": entry.session_key,
        "gateway_session_id": entry.session_id,
        "gateway_session_strict": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry", "with_adapter"),
    [
        (None, True),
        (_entry(origin=False), True),
        (_entry(), False),
    ],
)
async def test_dispatch_rejects_unroutable_session(entry, with_adapter):
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(entry, adapter if with_adapter else None)

    accepted = await runner._dispatch_plugin_message_injection(
        session_key="agent:main:telegram:dm:42",
        content="wake up",
        plugin_id="notify-plugin",
    )

    assert accepted is False
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_dispatch_rechecks_current_authorization(raises):
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(_entry(), adapter)
    if raises:
        runner._is_user_authorized.side_effect = RuntimeError("config unavailable")
    else:
        runner._is_user_authorized.return_value = False

    accepted = await runner._dispatch_plugin_message_injection(
        session_key="agent:main:telegram:dm:42",
        content="wake up",
        plugin_id="notify-plugin",
    )

    assert accepted is False
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_rejects_stored_role_only_authorization(monkeypatch):
    """A stored adapter role grant must be revalidated against current core auth."""
    for key in (
        "DISCORD_ALLOWED_USERS",
        "DISCORD_ALLOW_ALL_USERS",
        "GATEWAY_ALLOWED_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)

    adapter = MagicMock(spec=BasePlatformAdapter)
    adapter.handle_message = AsyncMock()
    entry = _entry()
    entry.session_key = "agent:main:discord:dm:42"
    entry.platform = Platform.DISCORD
    source = entry.origin
    assert source is not None
    source.platform = Platform.DISCORD
    source.role_authorized = True

    runner = _runner(entry)
    runner.adapters = {Platform.DISCORD: adapter}
    runner.config = GatewayConfig()
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    del runner._is_user_authorized

    accepted = await runner._dispatch_plugin_message_injection(
        session_key=entry.session_key,
        content="wake up",
        plugin_id="notify-plugin",
    )

    assert accepted is False
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_stops_when_gateway_drains_during_lookup():
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(_entry(), adapter)
    lookup_started = asyncio.Event()
    release_lookup = asyncio.Event()

    async def _lookup(_session_key):
        lookup_started.set()
        await release_lookup.wait()
        return _entry()

    runner._async_session_store.lookup_by_session_key = _lookup
    dispatch = asyncio.create_task(
        runner._dispatch_plugin_message_injection(
            session_key="agent:main:telegram:dm:42",
            content="wake up",
            plugin_id="notify-plugin",
        )
    )

    await lookup_started.wait()
    runner._draining = True
    release_lookup.set()

    assert await dispatch is False
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_base_adapter_queues_non_control_plugin_text_for_exact_session():
    adapter = _RoutingAdapter()
    adapter.set_message_handler(AsyncMock())
    source = _entry().origin
    session_key = build_session_key(source)
    adapter._active_sessions[session_key] = asyncio.Event()
    event = MessageEvent(
        text="/approve always",
        message_type=MessageType.TEXT,
        source=source,
        internal=True,
        allow_gateway_control=False,
        metadata={"gateway_session_key": session_key},
    )

    await adapter.handle_message(event)

    adapter._message_handler.assert_not_awaited()
    assert adapter._pending_messages[session_key] is event
    assert adapter._active_sessions[session_key].is_set() is False


@pytest.mark.asyncio
async def test_base_adapter_rejects_derived_session_mismatch():
    adapter = _RoutingAdapter()
    adapter.set_message_handler(AsyncMock())
    event = MessageEvent(
        text="ordinary input",
        source=_entry().origin,
        internal=True,
        allow_gateway_control=False,
        metadata={"gateway_session_key": "agent:main:telegram:dm:other"},
    )

    await adapter.handle_message(event)

    adapter._message_handler.assert_not_awaited()
    assert adapter._active_sessions == {}






@pytest.mark.asyncio
async def test_scheduler_logs_async_failure_without_callback_error(tmp_path):
    runner = _runner(_entry())
    loop = asyncio.get_running_loop()
    runner._gateway_loop = loop
    callback_errors = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: callback_errors.append(context))
    runner._dispatch_plugin_message_injection = AsyncMock(
        side_effect=RuntimeError("adapter failed")
    )
    runner.config = GatewayConfig()

    try:
        with patch("hermes_constants.get_routing_process_hermes_home", return_value=tmp_path):
            assert (
                runner._schedule_plugin_message_injection(
                    session_key="agent:main:telegram:dm:42",
                    content="wake up",
                    plugin_id="notify-plugin",
                    plugin_home=tmp_path,
                )
                is True
            )
        task = next(iter(runner._background_tasks))
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert callback_errors == []






def test_scheduler_rejects_stopped_or_closed_gateway():
    runner = _runner(_entry())
    loop = MagicMock()
    loop.is_closed.return_value = False
    runner._gateway_loop = loop
    runner._running = False

    assert (
        runner._schedule_plugin_message_injection(
            session_key="key",
            content="wake up",
            plugin_id="notify-plugin",
        )
        is False
    )
    loop.call_soon_threadsafe.assert_not_called()

    runner._running = True
    runner._gateway_loop = None
    assert (
        runner._schedule_plugin_message_injection(
            session_key="key",
            content="wake up",
            plugin_id="notify-plugin",
        )
        is False
    )

    runner._gateway_loop = loop
    loop.is_closed.return_value = True
    assert (
        runner._schedule_plugin_message_injection(
            session_key="key",
            content="wake up",
            plugin_id="notify-plugin",
        )
        is False
    )
    loop.call_soon_threadsafe.assert_not_called()


def test_scheduler_rejects_submission_failure(tmp_path):
    runner = _runner(_entry())
    runner.config = GatewayConfig()
    loop = MagicMock()
    loop.is_closed.return_value = False
    runner._gateway_loop = loop
    submitted = []

    def _reject(coro, _target_loop, **_kwargs):
        submitted.append(coro)
        coro.close()
        return None

    with patch("gateway.run.safe_schedule_threadsafe", side_effect=_reject), \
            patch("hermes_constants.get_routing_process_hermes_home", return_value=tmp_path):
        assert (
            runner._schedule_plugin_message_injection(
                session_key="agent:main:telegram:dm:42",
                content="wake up",
                plugin_id="notify-plugin",
                plugin_home=tmp_path,
            )
            is False
        )
    assert len(submitted) == 1  # refused by the submission, not by the profile check


def test_install_and_clear_gateway_injector_preserves_newer_owner():
    runner = _runner(_entry())
    manager = PluginManager()

    # The runner publishes process-wide; expose this standalone manager through the legacy slot.
    with patch("hermes_cli.plugins._plugin_manager", manager):
        runner._install_plugin_message_injector()
        assert manager.has_gateway_message_injector is True

        runner._clear_plugin_message_injector()
        assert manager.has_gateway_message_injector is False

        runner._install_plugin_message_injector()

        newer_owner = MagicMock()
        newer_injector = MagicMock(return_value=True)
        manager.set_gateway_message_injector(newer_owner, newer_injector)
        runner._clear_plugin_message_injector()

    assert manager.has_gateway_message_injector is True
    assert manager.inject_gateway_message(value="kept") is True
    newer_injector.assert_called_once_with(value="kept")


# ── WB-1127: client_message_id, plugin interrupt ────────────────────────────────────────────
# fork: werkbank-chat


def _plugin_home(tmp_path, monkeypatch, plugin_id="notify-plugin", allow=True):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(exist_ok=True)
    entry = {"allow_gateway_injection": True} if allow else {}
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"entries": {plugin_id: entry}}})
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


def _live_runner(store, adapter) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner.config = GatewayConfig()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._profile_adapters = {}
    runner._gateway_loop = asyncio.get_running_loop()
    runner._running = True
    runner._draining = False
    runner._background_tasks = set()
    runner._queued_events = {}
    runner._is_user_authorized = MagicMock(return_value=True)
    return runner


async def _drain(runner):
    while runner._background_tasks:
        await asyncio.gather(*list(runner._background_tasks), return_exceptions=True)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_two_injected_messages_with_distinct_ids_stay_separate_user_messages(tmp_path, monkeypatch):
    """Two Werkbank messages sent while a turn runs never fold into one: each is its own
    ``user.message`` carrying its own ``client_message_id`` (WB-1127)."""
    from gateway import session_events

    _plugin_home(tmp_path, monkeypatch)
    session_events._reset_for_tests()
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    source = _entry().origin
    entry = store.get_or_create_session(source)
    adapter = _RoutingAdapter()
    adapter.set_message_handler(AsyncMock())
    adapter._active_sessions[entry.session_key] = asyncio.Event()  # a turn is running
    runner = _live_runner(store, adapter)
    adapter.set_busy_session_handler(runner._handle_active_session_busy_message)

    manager = PluginManager()
    manager._discovered = True
    seen: list[dict] = []
    manager._hooks.setdefault(session_events.HOOK, []).append(lambda event, **_kw: seen.append(event))
    context = PluginContext(PluginManifest(name="notify-plugin", key="notify-plugin", source="user"), manager)

    try:
        with patch("hermes_cli.plugins._plugin_manager", manager):
            runner._install_plugin_message_injector()
            assert context.inject_message("erste", session_key=entry.session_key, client_message_id="wb-1") is True
            await _drain(runner)
            assert context.inject_message("zweite", session_key=entry.session_key, client_message_id="wb-2") is True
            await _drain(runner)
            assert session_events.wait_idle(5.0)
            runner._clear_plugin_message_injector()
    finally:
        session_events._reset_for_tests()

    head = adapter._pending_messages[entry.session_key]
    queued = runner._queued_events[entry.session_key]
    assert [head.text] + [e.text for e in queued] == ["erste", "zweite"]
    assert head.metadata["client_message_id"] == "wb-1"
    assert queued[0].metadata["client_message_id"] == "wb-2"
    users = [e for e in seen if e["type"] == "user.message"]
    assert [(e["id"], e["payload"]["client_message_id"], e["payload"]["text"]) for e in users] == [
        ("wb-1", "wb-1", "erste"), ("wb-2", "wb-2", "zweite")]
    assert all("merged_into" not in e["payload"] for e in users)
    assert {e["origin"] for e in users} == {"notify-plugin"}
    adapter._message_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_distinct_client_message_ids_never_share_a_pending_slot(tmp_path):
    """``client_message_id`` is a security-context key: even a photo burst (the one case the
    pending slot still merges) keeps two differently-identified messages apart."""
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    source = _entry().origin
    entry = store.get_or_create_session(source)
    adapter = _RoutingAdapter()
    runner = _live_runner(store, adapter)

    def _photo(cid, url):
        return MessageEvent(
            text="", message_type=MessageType.PHOTO, source=source, internal=True,
            allow_gateway_control=False, media_urls=[url], media_types=["image/jpeg"],
            metadata={"hermes_plugin_id": "notify-plugin", "hermes_plugin_injection": True,
                      "gateway_session_key": entry.session_key, "client_message_id": cid},
        )

    first, second = _photo("wb-1", "a.jpg"), _photo("wb-2", "b.jpg")
    runner._queue_or_replace_pending_event(entry.session_key, first)
    runner._queue_or_replace_pending_event(entry.session_key, second)

    assert adapter._pending_messages[entry.session_key] is first
    assert first.media_urls == ["a.jpg"]
    assert runner._queued_events[entry.session_key] == [second]


@pytest.mark.asyncio
async def test_dispatch_carries_client_message_id_into_metadata():
    adapter = SimpleNamespace(handle_message=AsyncMock())
    entry = _entry()
    runner = _runner(entry, adapter)

    assert await runner._dispatch_plugin_message_injection(
        session_key=entry.session_key, content="hallo", plugin_id="werkbank", client_message_id="wb-7",
    ) is True
    assert adapter.handle_message.await_args.args[0].metadata["client_message_id"] == "wb-7"


def _stop_runner(entry, adapter=None):
    runner = _runner(entry, adapter or SimpleNamespace(send=AsyncMock()))
    runner.config = GatewayConfig()
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._running_agents = {}
    runner._pending_messages = {}
    return runner


@pytest.mark.asyncio
async def test_plugin_interrupt_session_stops_running_turn_like_stop(tmp_path, monkeypatch):
    from gateway.run import _INTERRUPT_REASON_STOP

    home = _plugin_home(tmp_path, monkeypatch)
    entry = _entry()
    runner = _stop_runner(entry)
    runner._gateway_loop = asyncio.get_running_loop()
    agent = MagicMock()
    runner._running_agents[entry.session_key] = agent
    manager = PluginManager(scope_key=str(home))
    context = PluginContext(PluginManifest(name="notify-plugin", key="notify-plugin", source="user"), manager)

    with patch("hermes_cli.plugins._plugin_manager", manager), \
            patch("hermes_constants.get_routing_process_hermes_home", return_value=home), \
            patch("hermes_cli.plugins.invoke_hook") as hook:
        runner._install_plugin_message_injector()
        assert context.interrupt_session(entry.session_key) is True
        await _drain(runner)
        runner._clear_plugin_message_injector()

    # Same hard interrupt /stop sends (a control reason, never replayed as user text) ...
    agent.interrupt.assert_called_once_with(_INTERRUPT_REASON_STOP)
    # ... and the hook names the plugin as the issuer.
    hook.assert_any_call(
        "agent_loop_stopped", session_key=entry.session_key, platform="telegram",
        reason="plugin:notify-plugin", invalidation_reason="plugin_interrupt",
    )
    assert entry.session_key not in runner._running_agents


@pytest.mark.asyncio
async def test_plugin_interrupt_session_needs_allow_gateway_injection(tmp_path, monkeypatch):
    home = _plugin_home(tmp_path, monkeypatch, allow=False)
    entry = _entry()
    runner = _stop_runner(entry)
    runner._gateway_loop = asyncio.get_running_loop()
    agent = MagicMock()
    runner._running_agents[entry.session_key] = agent
    manager = PluginManager(scope_key=str(home))
    context = PluginContext(PluginManifest(name="notify-plugin", key="notify-plugin", source="user"), manager)

    with patch("hermes_cli.plugins._plugin_manager", manager), \
            patch("hermes_constants.get_routing_process_hermes_home", return_value=home), \
            patch("hermes_cli.plugins.invoke_hook") as hook:
        runner._install_plugin_message_injector()
        assert context.interrupt_session(entry.session_key) is False
        await _drain(runner)
        runner._clear_plugin_message_injector()

    agent.interrupt.assert_not_called()
    assert runner._running_agents[entry.session_key] is agent
    assert not [c for c in hook.call_args_list if c.args and c.args[0] == "agent_loop_stopped"]


@pytest.mark.asyncio
async def test_plugin_interrupt_session_refuses_other_profiles_sessions(tmp_path, monkeypatch):
    """Security: a plugin stops only sessions of its own profile (multiplex: the key namespace;
    single-profile: a plugin of another home is not served by this gateway at all)."""
    import gateway.run as gateway_run

    coder_home, other_home = tmp_path / "coder", tmp_path / "other"
    coder_home.mkdir()
    other_home.mkdir()
    entry = _entry()
    runner = _stop_runner(entry)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._gateway_loop = asyncio.get_running_loop()
    monkeypatch.setattr(gateway_run, "_multiplex_profile_homes",
                        lambda _cfg: [("coder", coder_home), ("other", other_home)])
    foreign, own = "agent:other:telegram:dm:42", "agent:coder:telegram:dm:42"
    foreign_agent, own_agent = MagicMock(), MagicMock()
    runner._running_agents[foreign] = foreign_agent
    runner._running_agents[own] = own_agent

    with patch("hermes_cli.plugins.invoke_hook"):
        assert runner._schedule_plugin_session_interrupt(
            session_key=foreign, plugin_id="werkbank", plugin_home=coder_home) is False
        assert runner._schedule_plugin_session_interrupt(
            session_key="not-a-gateway-key", plugin_id="werkbank", plugin_home=coder_home) is False
        assert runner._schedule_plugin_session_interrupt(
            session_key=own, plugin_id="werkbank", plugin_home=tmp_path / "unserved") is False
        await _drain(runner)
        foreign_agent.interrupt.assert_not_called()
        own_agent.interrupt.assert_not_called()

        assert runner._schedule_plugin_session_interrupt(
            session_key=own, plugin_id="werkbank", plugin_home=coder_home) is True
        await _drain(runner)
    own_agent.interrupt.assert_called_once()
    foreign_agent.interrupt.assert_not_called()

    # Single-profile gateway: a plugin manager of another home cannot reach its sessions.
    runner.config = GatewayConfig()
    runner._running_agents[entry.session_key] = foreign_agent
    with patch("hermes_constants.get_routing_process_hermes_home", return_value=coder_home):
        assert runner._schedule_plugin_session_interrupt(
            session_key=entry.session_key, plugin_id="werkbank", plugin_home=other_home) is False
    await _drain(runner)
    foreign_agent.interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_plugin_interrupt_session_without_running_turn_is_false(tmp_path):
    entry = _entry()
    runner = _stop_runner(entry)
    runner._gateway_loop = asyncio.get_running_loop()
    with patch("hermes_constants.get_routing_process_hermes_home", return_value=tmp_path):
        assert runner._schedule_plugin_session_interrupt(
            session_key=entry.session_key, plugin_id="werkbank", plugin_home=tmp_path) is False
    assert runner._background_tasks == set()


@pytest.mark.asyncio
async def test_plugin_inject_message_refuses_other_profiles_sessions(tmp_path, monkeypatch):
    """Security (WB-1127 review): inject_message(session_key=...) reaches only sessions of the
    plugin's own profile — same rule as interrupt_session."""
    import gateway.run as gateway_run

    coder_home, other_home = tmp_path / "coder", tmp_path / "other"
    coder_home.mkdir()
    other_home.mkdir()
    runner = _runner(_entry())
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._gateway_loop = asyncio.get_running_loop()
    runner._dispatch_plugin_message_injection = AsyncMock(return_value=True)
    monkeypatch.setattr(gateway_run, "_multiplex_profile_homes",
                        lambda _cfg: [("coder", coder_home), ("other", other_home)])
    foreign, own = "agent:other:telegram:dm:42", "agent:coder:telegram:dm:42"

    def _inject(key, home):
        return runner._schedule_plugin_message_injection(
            session_key=key, content="hallo", plugin_id="werkbank", plugin_home=home)

    assert _inject(foreign, coder_home) is False
    assert _inject("not-a-gateway-key", coder_home) is False
    assert _inject(own, tmp_path / "unserved") is False
    assert _inject(own, None) is False
    await _drain(runner)
    runner._dispatch_plugin_message_injection.assert_not_awaited()

    assert _inject(own, coder_home) is True
    await _drain(runner)
    runner._dispatch_plugin_message_injection.assert_awaited_once()
    assert runner._dispatch_plugin_message_injection.await_args.kwargs["session_key"] == own

    # Single-profile gateway: a plugin manager of another home cannot reach its sessions.
    runner.config = GatewayConfig()
    runner._dispatch_plugin_message_injection.reset_mock()
    with patch("hermes_constants.get_routing_process_hermes_home", return_value=coder_home):
        assert _inject("agent:main:telegram:dm:42", other_home) is False
    await _drain(runner)
    runner._dispatch_plugin_message_injection.assert_not_awaited()


@pytest.mark.asyncio
async def test_photo_follow_ups_without_client_message_id_still_merge(tmp_path):
    """Regression: without ``client_message_id`` a photo burst keeps its album semantics — both
    photos fold into the one pending slot (run_busy._queue_or_replace_pending_event)."""
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    source = _entry().origin
    entry = store.get_or_create_session(source)
    adapter = _RoutingAdapter()
    runner = _live_runner(store, adapter)

    def _photo(url):
        return MessageEvent(text="", message_type=MessageType.PHOTO, source=source,
                            media_urls=[url], media_types=["image/jpeg"])

    first, second = _photo("a.jpg"), _photo("b.jpg")
    runner._queue_or_replace_pending_event(entry.session_key, first)
    runner._queue_or_replace_pending_event(entry.session_key, second)

    assert adapter._pending_messages[entry.session_key] is first
    assert first.media_urls == ["a.jpg", "b.jpg"]
    assert not runner._queued_events.get(entry.session_key)


@pytest.mark.asyncio
async def test_plain_stop_still_reports_stop_requested():
    """Regression: a user's /stop (no ``hook_reason``) keeps ``agent_loop_stopped`` reason
    'Stop requested'; only the plugin path renames it."""
    from gateway.run import _INTERRUPT_REASON_STOP

    entry = _entry()
    runner = _stop_runner(entry)
    agent = MagicMock()
    runner._running_agents[entry.session_key] = agent
    event = MessageEvent(text="/stop", message_type=MessageType.TEXT, source=entry.origin)

    with patch("hermes_cli.plugins.invoke_hook") as hook:
        await runner._busy_stop_command(event, entry.session_key, entry.origin)

    assert _INTERRUPT_REASON_STOP == "Stop requested"
    agent.interrupt.assert_called_once_with("Stop requested")
    hook.assert_any_call(
        "agent_loop_stopped", session_key=entry.session_key, platform="telegram",
        reason="Stop requested", invalidation_reason="stop_command",
    )


# ── WB-1174: media attachments from a plugin ──────────────────────────────────────────────────
# fork: werkbank-chat


@pytest.mark.asyncio
async def test_dispatch_carries_media_as_a_photo_event():
    adapter = SimpleNamespace(handle_message=AsyncMock())
    entry = _entry()
    runner = _runner(entry, adapter)

    assert await runner._dispatch_plugin_message_injection(
        session_key=entry.session_key, content="was siehst du?", plugin_id="werkbank", client_message_id="wb-8",
        media_urls=["/h/cache/images/img_1.png"], media_types=["image/png"],
    ) is True
    event = adapter.handle_message.await_args.args[0]
    assert event.message_type == MessageType.PHOTO
    assert event.media_urls == ["/h/cache/images/img_1.png"]
    assert event.media_types == ["image/png"]
    assert event.metadata["client_message_id"] == "wb-8"


@pytest.mark.asyncio
async def test_dispatch_with_a_document_is_a_document_event_not_inlined():
    adapter = SimpleNamespace(handle_message=AsyncMock())
    entry = _entry()
    runner = _runner(entry, adapter)

    assert await runner._dispatch_plugin_message_injection(
        session_key=entry.session_key, content="", plugin_id="werkbank",
        media_urls=["/h/cache/images/img_1.png", "/h/cache/documents/doc_1_bericht.pdf"],
        media_types=["image/png", "application/pdf"],
    ) is True
    event = adapter.handle_message.await_args.args[0]
    assert event.message_type == MessageType.DOCUMENT
    assert event.media_text_inlined == [False, False]     # the content is NOT in the text: read the file


@pytest.mark.asyncio
async def test_dispatch_without_media_is_the_plain_text_event_as_before():
    adapter = SimpleNamespace(handle_message=AsyncMock())
    entry = _entry()
    runner = _runner(entry, adapter)

    assert await runner._dispatch_plugin_message_injection(
        session_key=entry.session_key, content="nur text", plugin_id="werkbank") is True
    event = adapter.handle_message.await_args.args[0]
    assert event.message_type == MessageType.TEXT
    assert event.media_urls == [] and event.media_types == [] and event.media_text_inlined == []


@pytest.mark.asyncio
async def test_plugin_context_media_reach_the_live_gateway_as_a_photo_event(tmp_path, monkeypatch):
    """The whole seam: ``PluginContext.inject_message(media_urls=…)`` → published injector → dispatch →
    the session's adapter holds a PHOTO event with the cached file (a turn runs, so it is queued)."""
    hermes_home = tmp_path / "hermes"
    (hermes_home / "cache" / "images").mkdir(parents=True)
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"entries": {"notify-plugin": {"allow_gateway_injection": True}}}}))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    image = hermes_home / "cache" / "images" / "img_abc.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)

    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    entry = store.get_or_create_session(_entry().origin)
    adapter = _RoutingAdapter()
    adapter.set_message_handler(AsyncMock())
    adapter._active_sessions[entry.session_key] = asyncio.Event()
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._profile_adapters = {}
    runner._gateway_loop = asyncio.get_running_loop()
    runner._running = True
    runner._draining = False
    runner._background_tasks = set()
    runner._queued_events = {}
    runner._is_user_authorized = MagicMock(return_value=True)
    adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
    manager = PluginManager()
    context = PluginContext(PluginManifest(name="notify-plugin", key="notify-plugin", source="user"), manager)

    with patch("hermes_cli.plugins._plugin_manager", manager):
        runner._install_plugin_message_injector()
        assert context.inject_message("was siehst du?", session_key=entry.session_key, client_message_id="wb-9",
                                      media_urls=[str(image)], media_types=["image/png"]) is True
        await asyncio.gather(*list(runner._background_tasks), return_exceptions=True)
        await asyncio.sleep(0)
        runner._clear_plugin_message_injector()

    held = [adapter._pending_messages.get(entry.session_key)] + list(runner._queued_events.get(entry.session_key, []))
    event = next(e for e in held if e is not None and e.metadata.get("client_message_id") == "wb-9")
    assert event.message_type == MessageType.PHOTO
    assert event.media_urls == [str(image)] and event.media_types == ["image/png"]
    assert event.text == "was siehst du?" and event.internal is True

"""One werkbank server per gateway process, alive while any context of the plugin is loaded.

Live finding 2026-10-14 (gateway restarts 06:30 and 06:38): every start logged ``cannot listen`` AND
``listening`` on 8767 in the same millisecond; afterwards the board got 401 (06:30) or nobody listened
(06:38). Causes, each reproduced here through the real code path:

1. ``hermes gateway status`` (polled every second by the restart script while the gateway boots) imports
   ``gateway.run`` for its credential check — which sets ``_HERMES_GATEWAY=1`` at import — and then
   resolves every platform plugin (``load_gateway_config`` → ``shared_loop_targets`` →
   ``platform_registry.plugin_entries``). The plugin took that CLI for the gateway, wrote/raced the key
   and grabbed the port; the CLI exited a moment later and the port was dead.
2. A forced re-discovery inside the gateway (``reload-plugins``, plugin go-live, the tts/stt/image
   retries) unloads the plugin (server stopped) but only re-arms its LAZY loader; nothing looks up an
   adapterless platform again, so the server stayed down until the next restart.
3. The start guard was a module global, but every scope/reload imports this package as a new module, and
   the guard read ``server.closing`` (a property) as if it were a method.
"""

from __future__ import annotations

import http.client
import socket
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from werkbank_testkit import call

PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "platforms" / "werkbank"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def _write_config(home: Path, port: int) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        f"plugins:\n  entries:\n    werkbank:\n      enabled: true\n      port: {port}\n")


def _file_key(home: Path) -> str:
    return (home / "plugin-data" / "werkbank" / "key").read_text().strip()


@pytest.fixture
def gateway_run(monkeypatch):
    """The real ``gateway.run`` module, imported the way a CLI imports it (marker env set on import)."""
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    import gateway.run as run
    return run


@pytest.fixture
def fresh_runtime(monkeypatch):
    """No server registry left over from another test; every server started here is stopped."""
    monkeypatch.delitem(sys.modules, "hermes_werkbank_runtime", raising=False)
    yield
    rt = sys.modules.get("hermes_werkbank_runtime")
    for inst in list(getattr(rt, "instances", {}).values()):
        inst.server.stop()


class Ctx:
    """A plugin context as the loader hands it over: hooks and unload callbacks are recorded."""

    plugin_id = "platforms/werkbank"

    def __init__(self, home: Path):
        self._manager = SimpleNamespace(_gateway_message_injector=None, home_path=home)
        self.hooks: dict = {}
        self.unloads: list = []

    def register_hook(self, name, callback):
        self.hooks.setdefault(name, []).append(callback)

    def on_unload(self, callback):
        self.unloads.append(callback)

    def inject_message(self, content, role="user", *, session_key=None, client_message_id=None):
        return True

    def unload(self):
        for cb in self.unloads:
            cb()


def _load_copy(home: Path, module_name: str) -> types.ModuleType:
    """Import the plugin package through the REAL loader under ``module_name`` — what the plugin
    manager does once per scope and again on every reload."""
    from hermes_cli.plugins import PluginManager
    from hermes_cli.plugins_discovery import collect_directory_manifests

    manifest = next(m for m in collect_directory_manifests() if m.key == "platforms/werkbank")
    return PluginManager(scope_key=str(home))._load_directory_module(manifest, module_name=module_name)


# ── 1. a CLI that imported gateway.run is not the gateway ────────────────────────────────────


def test_importing_gateway_run_does_not_make_a_cli_the_gateway(gateway_run, monkeypatch):
    import plugins.platforms.werkbank as plugin

    assert plugin.in_gateway_process() is False          # `hermes gateway status`: imported, not started
    monkeypatch.setattr(gateway_run, "_GATEWAY_PROCESS_STARTED", True)
    assert plugin.in_gateway_process() is True           # start_gateway() ran in this process


def test_start_gateway_sets_the_process_marker(gateway_run, monkeypatch):
    """The marker is set at the very top of start_gateway(), before the runner resolves plugins."""
    import inspect

    src = inspect.getsource(gateway_run.start_gateway)
    first_load = src.index("GatewayRunner(config)")
    assert 0 < src.index("_GATEWAY_PROCESS_STARTED = True") < first_load


def test_gateway_status_path_opens_no_port(gateway_run, tmp_path, monkeypatch, fresh_runtime):
    """Replays the status CLI: real plugin manager, real deferred loader, real platform lookup."""
    from gateway.platform_registry import platform_registry
    from hermes_cli.plugins import _reset_plugin_managers_for_tests, get_plugin_manager

    home = tmp_path / "home"
    port = _free_port()
    _write_config(home, port)
    monkeypatch.setenv("HERMES_HOME", str(home))
    _reset_plugin_managers_for_tests()
    try:
        get_plugin_manager().discover_and_load()
        platform_registry.plugin_entries()               # what load_gateway_config does in `gateway status`
        assert not _listening(port)
        assert not (home / "plugin-data" / "werkbank" / "key").exists()
    finally:
        _reset_plugin_managers_for_tests()


# ── 2. two copies of the package, one server ─────────────────────────────────────────────────


def test_two_module_copies_share_one_server_until_the_last_unloads(gateway_run, tmp_path, monkeypatch,
                                                                   fresh_runtime):
    monkeypatch.setattr(gateway_run, "_GATEWAY_PROCESS_STARTED", True)
    home = tmp_path / "home"
    port = _free_port()
    _write_config(home, port)
    first = _load_copy(home, "hermes_plugins.platforms__werkbank__t1")
    second = _load_copy(home, "hermes_plugins.platforms__werkbank__t2")
    try:
        assert first is not second and first.register is not second.register
        ctx_a, ctx_b = Ctx(home), Ctx(home)
        first.register(ctx_a)
        second.register(ctx_b)

        key = _file_key(home)
        assert call(port, "GET", "/v1/resolve?session_id=x_1", key)[0] in (404, 503)
        assert call(port, "GET", "/v1/resolve?session_id=x_1", "wrong")[0] == 401
        # Both contexts hold the server: hook + unload in each.
        assert len(ctx_a.hooks["on_gateway_session_event"]) == 1 and len(ctx_a.unloads) == 1
        assert len(ctx_b.hooks["on_gateway_session_event"]) == 1 and len(ctx_b.unloads) == 1

        ctx_a.unload()                                   # the older context goes (a reload, a scope)
        assert _listening(port), "the server died although a context of the plugin is still loaded"
        assert call(port, "GET", "/v1/resolve?session_id=x_1", key)[0] in (404, 503)
        inst = sys.modules["hermes_werkbank_runtime"].instances[str(home.resolve())]
        assert inst.host.ctx is ctx_b                    # the host no longer serves the unloaded ctx

        ctx_b.unload()                                   # the last one: the port closes
        assert not _listening(port)
    finally:
        for name in ("hermes_plugins.platforms__werkbank__t1", "hermes_plugins.platforms__werkbank__t2"):
            for mod in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
                del sys.modules[mod]


def test_only_the_newest_holder_feeds_the_buffers(gateway_run, tmp_path, monkeypatch, fresh_runtime):
    import plugins.platforms.werkbank as plugin

    monkeypatch.setattr(gateway_run, "_GATEWAY_PROCESS_STARTED", True)
    home = tmp_path / "home"
    _write_config(home, _free_port())
    ctx_a, ctx_b = Ctx(home), Ctx(home)
    plugin.register(ctx_a)
    plugin.register(ctx_b)
    inst = sys.modules["hermes_werkbank_runtime"].instances[str(home.resolve())]
    seen = []
    monkeypatch.setattr(inst.buffers, "on_event", seen.append)
    for ctx in (ctx_a, ctx_b):
        ctx.hooks["on_gateway_session_event"][0](event={"n": 1})
    assert seen == [{"n": 1}]
    ctx_b.unload()
    ctx_a.hooks["on_gateway_session_event"][0](event={"n": 2})
    assert seen == [{"n": 1}, {"n": 2}]
    ctx_a.unload()


def test_a_stopped_server_is_started_again(gateway_run, tmp_path, monkeypatch, fresh_runtime):
    import plugins.platforms.werkbank as plugin

    monkeypatch.setattr(gateway_run, "_GATEWAY_PROCESS_STARTED", True)
    home = tmp_path / "home"
    port = _free_port()
    _write_config(home, port)
    ctx = Ctx(home)
    plugin.register(ctx)
    sys.modules["hermes_werkbank_runtime"].instances[str(home.resolve())].server.stop()
    assert not _listening(port)
    plugin.register(Ctx(home))
    assert _listening(port)


# ── 3. a forced re-discovery in the gateway keeps the server ─────────────────────────────────


def test_force_rediscovery_brings_the_server_back(gateway_run, tmp_path, monkeypatch, fresh_runtime):
    """``reload-plugins`` / go-live / the tts-stt-image retries: real manager, real ledger unload."""
    from gateway.platform_registry import platform_registry
    from hermes_cli.plugins import _reset_plugin_managers_for_tests, get_plugin_manager

    monkeypatch.setattr(gateway_run, "_GATEWAY_PROCESS_STARTED", True)
    home = tmp_path / "home"
    port = _free_port()
    _write_config(home, port)
    monkeypatch.setenv("HERMES_HOME", str(home))
    _reset_plugin_managers_for_tests()
    try:
        manager = get_plugin_manager()
        manager.discover_and_load()
        platform_registry.plugin_entries()               # the gateway runner's startup lookup
        assert _listening(port)
        key = _file_key(home)

        manager.discover_and_load(force=True)
        assert _listening(port), "a forced re-discovery left the werkbank server stopped"
        assert call(port, "GET", "/v1/resolve?session_id=x_1", key)[0] in (404, 503)
        loaded = manager._plugins["platforms/werkbank"]
        assert loaded.enabled and not loaded.deferred
    finally:
        _reset_plugin_managers_for_tests()
    assert not _listening(port)                          # unloading the manager stops it


def test_force_rediscovery_leaves_never_used_platforms_lazy(tmp_path, monkeypatch):
    from hermes_cli.plugins import _reset_plugin_managers_for_tests, get_plugin_manager

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    _reset_plugin_managers_for_tests()
    try:
        manager = get_plugin_manager()
        manager.discover_and_load()
        manager.discover_and_load(force=True)
        assert manager._plugins["platforms/werkbank"].deferred
    finally:
        _reset_plugin_managers_for_tests()

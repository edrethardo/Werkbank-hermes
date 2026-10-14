"""werkbank — the Werkbank board's window into a Hermes gateway session (WB-1128).

A platform plugin WITHOUT a messaging adapter: it registers no platform, only

* the ``on_gateway_session_event`` hook (``gateway/session_events.py``), which fills a ring
  buffer per session the board asked for, and
* a small HTTP server on ``127.0.0.1:<port>`` speaking the contract in ``PROTOCOL.md``
  (resolve, snapshot, event stream with replay, send, stop).

It runs only inside ``hermes gateway`` and only with ``plugins.entries.werkbank.enabled: true``;
the CLI, the TUI and ``hermes plugins`` load it and do nothing. See ``README.md`` for the config.
"""

from __future__ import annotations

import functools
import logging
import os
import sys
import threading
import types
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger("plugins.platforms.werkbank")

HOOK = "on_gateway_session_event"

# One server per Hermes home and process. The state must NOT live in a module global: the loader imports
# this package under a fresh module name per scope and re-executes it on every reload
# (``hermes_plugins.platforms__werkbank``, ``…__home_<digest>``), so each copy would see its own empty
# dict. A fixed ``sys.modules`` entry is the one place every copy shares.
RUNTIME_MODULE = "hermes_werkbank_runtime"


def runtime() -> types.ModuleType:
    """The process-wide state shared by every copy of this package: ``lock`` and ``instances``."""
    rt = sys.modules.get(RUNTIME_MODULE)
    if rt is None:
        fresh = types.ModuleType(RUNTIME_MODULE, "werkbank plugin: process-wide server registry")
        fresh.lock = threading.RLock()        # type: ignore[attr-defined]
        fresh.instances = {}                  # type: ignore[attr-defined]  # home -> Instance
        rt = sys.modules.setdefault(RUNTIME_MODULE, fresh)   # atomic: two copies racing get one object
    return rt


class Instance:
    """A running server and the plugin contexts that hold it: it stops when the LAST one unloads."""

    def __init__(self, server: Any, buffers: Any, host: Any, home_key: str) -> None:
        self.server = server
        self.buffers = buffers
        self.host = host
        self.home_key = home_key
        self.contexts: List[Any] = []        # attach order; the newest feeds the buffers and serves the host


def in_gateway_process() -> bool:
    """True only inside a running ``hermes gateway``. The ``_HERMES_GATEWAY`` marker alone is not enough:
    gateway/run.py sets it on IMPORT, and CLI commands import that module incidentally (``hermes gateway
    status`` does, for its credential check, and then resolves every platform plugin) — that CLI would
    open the port and race the gateway for it. ``gateway_process_started()`` turns true only in
    ``start_gateway()``."""
    if os.environ.get("_HERMES_GATEWAY") != "1":
        return False
    started = getattr(sys.modules.get("gateway.run"), "gateway_process_started", None)
    return bool(callable(started) and started())


def plugin_home(ctx: Any) -> Path:
    home = getattr(getattr(ctx, "_manager", None), "home_path", None)
    if home:
        return Path(home)
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _launch(ctx: Any, home: Path, settings, *, host: Any = None, port: Optional[int] = None) -> Optional[Instance]:
    """Key file, buffers, server. ``None`` (logged, no text) when the key or the port is not usable."""
    from . import buffer, keyfile, server
    from .host import GatewayHost

    try:
        key = keyfile.ensure_key(home)
    except (OSError, keyfile.KeyFileError) as exc:
        logger.warning("werkbank: key file not usable (%s)", type(exc).__name__)
        return None
    host = host or GatewayHost(ctx, home)
    buffers = buffer.Buffers(plugin_id=getattr(ctx, "plugin_id", "platforms/werkbank"),
                             watermark=getattr(host, "row_watermark", None))
    mirror = bool(settings.mirror_to_telegram)
    try:
        srv = server.WerkbankServer(key, buffers, host, settings.port if port is None else port,
                                    mirror_enabled=lambda: mirror).start()
    except OSError as exc:
        logger.warning("werkbank: cannot listen on 127.0.0.1:%s (%s)", settings.port, type(exc).__name__)
        return None
    logger.info("werkbank: listening on 127.0.0.1:%s (client_message_id=%s, interrupt=%s, mirror=%s)",
                srv.port, getattr(host, "supports_client_message_id", None),
                getattr(host, "supports_interrupt", None), mirror)
    return Instance(srv, buffers, host, str(Path(home).resolve()))


def _serve_host_from(inst: Instance, ctx: Any) -> None:
    rebind = getattr(inst.host, "rebind", None)
    if callable(rebind):
        rebind(ctx)


def attach(inst: Instance, ctx: Any) -> None:
    """Hold ``inst`` from ``ctx``: the hook feeds its buffers, ``ctx``'s unload releases it."""
    with runtime().lock:
        inst.contexts.append(ctx)
        _serve_host_from(inst, ctx)

    def on_gateway_session_event(event=None, **_kw):
        # Only the newest holder delivers: two live contexts must not put every event in twice.
        holders = inst.contexts
        if holders and holders[-1] is ctx:
            inst.buffers.on_event(event)

    ctx.register_hook(HOOK, on_gateway_session_event)
    ctx.on_unload(functools.partial(release, inst, ctx))


def release(inst: Instance, ctx: Any) -> None:
    """``ctx`` unloads: hand the host to the next holder, or stop the server when none is left."""
    rt = runtime()
    with rt.lock:
        inst.contexts[:] = [c for c in inst.contexts if c is not ctx]
        if inst.contexts:
            _serve_host_from(inst, inst.contexts[-1])
            return
        if rt.instances.get(inst.home_key) is inst:
            del rt.instances[inst.home_key]
        inst.server.stop()      # synchronous: the port is free when this returns


def start(ctx: Any, home: Path, settings, *, host: Any = None, port: Optional[int] = None):
    """A server held by ``ctx`` alone (not in the process registry). Returns the server or ``None``."""
    inst = _launch(ctx, home, settings, host=host, port=port)
    if inst is None:
        return None
    attach(inst, ctx)
    return inst.server


def register(ctx: Any) -> None:
    if not in_gateway_process():
        return
    if getattr(ctx, "_load_abandoned", False):
        return      # a timed-out load: the loader ignores this context's hooks and unload callbacks
    from . import config
    home = plugin_home(ctx)
    settings = config.read(home)
    if not settings.enabled:
        logger.debug("werkbank: off (plugins.entries.werkbank.enabled is not true)")
        return
    if settings.problem:
        logger.warning("werkbank: not started: plugins.entries.werkbank: %s", settings.problem)
        return
    rt = runtime()
    home_key = str(home.resolve())
    with rt.lock:
        inst = rt.instances.get(home_key)
        if inst is not None and inst.server.closing:
            del rt.instances[home_key]
            inst = None
        if inst is None:
            inst = _launch(ctx, home, settings)
            if inst is None:
                return
            rt.instances[home_key] = inst
        else:
            logger.debug("werkbank: one more plugin context holds the server on port %s", inst.server.port)
        attach(inst, ctx)

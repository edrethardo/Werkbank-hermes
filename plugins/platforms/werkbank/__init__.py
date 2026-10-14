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

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("plugins.platforms.werkbank")

HOOK = "on_gateway_session_event"

_STARTED: dict = {}
_STARTED_LOCK = threading.Lock()


def in_gateway_process() -> bool:
    """True only inside ``hermes gateway``: gateway/run.py sets the marker at import, and the gateway
    module must be loaded in THIS interpreter (a child the gateway spawns inherits the marker)."""
    return os.environ.get("_HERMES_GATEWAY") == "1" and "gateway.run" in sys.modules


def plugin_home(ctx: Any) -> Path:
    home = getattr(getattr(ctx, "_manager", None), "home_path", None)
    if home:
        return Path(home)
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def start(ctx: Any, home: Path, settings, *, host: Any = None, port: Optional[int] = None):
    """Key file, buffers, hook, server. Returns the server, or ``None`` (logged, no text)."""
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

    def on_gateway_session_event(event=None, **_kw):
        buffers.on_event(event)

    ctx.register_hook(HOOK, on_gateway_session_event)
    ctx.on_unload(srv.stop)
    logger.info("werkbank: listening on 127.0.0.1:%s (client_message_id=%s, interrupt=%s, mirror=%s)",
                srv.port, getattr(host, "supports_client_message_id", None),
                getattr(host, "supports_interrupt", None), mirror)
    return srv


def register(ctx: Any) -> None:
    if not in_gateway_process():
        return
    from . import config
    home = plugin_home(ctx)
    settings = config.read(home)
    if not settings.enabled:
        logger.debug("werkbank: off (plugins.entries.werkbank.enabled is not true)")
        return
    if settings.problem:
        logger.warning("werkbank: not started: plugins.entries.werkbank: %s", settings.problem)
        return
    # The gateway may register this plugin from more than one scope; one server per process.
    with _STARTED_LOCK:
        running = _STARTED.get(home.resolve())
        closing = getattr(running, "closing", None)
        if running is not None and not (callable(closing) and closing()):
            logger.debug("werkbank: already running in this process for %s", home)
            return
        srv = start(ctx, home, settings)
        if srv is not None:
            _STARTED[home.resolve()] = srv

"""key-prompt: an API key typed on a dashboard page, stored without passing through shell or chat.

``request_api_key`` (agent tool) opens a one-shot request and returns a link to
``/p/schluessel/?r=<id>``; the owner types the key there and the dashboard process stores it
directly in the chosen profile's credential pool. ``api_key_request_status`` reports
stored/cancelled/expired + label, never the value.

Why its own plugin and not part of ``sudo-prompt``: sudo hands a password to a *waiting*
process over a socket and never stores it; here nothing waits and the dashboard itself
persists the value. Different lifecycle, different trust edge — only the page pattern is shared.
"""

from __future__ import annotations

import logging

log = logging.getLogger("hermes_plugins.key_prompt")


def _public_url(ctx) -> str:
    configured = ctx.get_config("public_url")
    if configured:
        return str(configured)
    try:
        from hermes_cli.config import load_config_readonly
        return str(((load_config_readonly() or {}).get("dashboard") or {}).get("public_url") or "")
    except Exception:
        return ""


def register(ctx) -> None:
    from . import tools
    from .store import DEFAULT_TIMEOUT

    # Page: only where a dashboard mounts it; harmless elsewhere.
    try:
        from .page import build_router
        ctx.register_dashboard_page(slug=tools.SLUG, title="API-Schlüssel", router=build_router())
    except Exception:
        log.debug("key-prompt: no dashboard page surface here", exc_info=True)

    try:
        timeout = int(ctx.get_config("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    url = _public_url(ctx)
    if not url:
        log.warning("key-prompt: no dashboard.public_url — request_api_key will refuse to build links")
    tools.configure(url, timeout)

    ctx.register_tool(name="request_api_key", toolset="key_prompt",
                      schema=tools.REQUEST_API_KEY_SCHEMA, handler=tools.request_api_key,
                      description="One-shot dashboard link for the owner to enter an API key",
                      emoji="🔑")
    ctx.register_tool(name="api_key_request_status", toolset="key_prompt",
                      schema=tools.API_KEY_REQUEST_STATUS_SCHEMA,
                      handler=tools.api_key_request_status,
                      description="Outcome of a request_api_key link (never the key)", emoji="🔑")

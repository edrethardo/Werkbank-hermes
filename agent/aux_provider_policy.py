"""Which main providers ``auto`` side tasks must not route back onto (WB-1216).

``auxiliary.<task>.provider: auto`` normally means "my main model for side tasks too". For a
trial endpoint that is wrong: NVIDIA Build (``integrate.api.nvidia.com``) is licensed for
development/test load only, and every side task (title prompt, compression summary, vision,
background memory/skill review) would send another copy of session data there.

``auxiliary.exclude_main_providers`` lists such main providers. Unset → ``["nvidia"]`` (only
the hosted endpoint; a local NIM behind ``NVIDIA_BASE_URL`` is not the trial). ``[]`` restores
main-model-first for every provider. An excluded main provider's side tasks take an explicit
``auxiliary.<task>`` route, else the fallback chain (minus excluded providers), else stay off.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, FrozenSet, Optional

logger = logging.getLogger(__name__)

CONFIG_KEY = "exclude_main_providers"
DEFAULT_EXCLUDED: tuple[str, ...] = ("nvidia",)
HOSTED_NIM_HOST = "integrate.api.nvidia.com"


def _load_config() -> Dict[str, Any]:
    from hermes_cli.config import load_config_readonly
    return load_config_readonly() or {}


def _canonical(provider: Optional[str]) -> str:
    name = str(provider or "").strip().lower()
    if not name:
        return ""
    try:
        from hermes_cli.providers import normalize_provider
        return (normalize_provider(name) or name).strip().lower()
    except Exception:
        return name


def excluded_main_providers() -> FrozenSet[str]:
    """Canonical provider ids from ``auxiliary.exclude_main_providers``; safe default on any error."""
    try:
        aux = _load_config().get("auxiliary") or {}
        raw = aux.get(CONFIG_KEY) if isinstance(aux, dict) else None
    except Exception:
        logger.debug("auxiliary.%s unreadable; keeping default %s", CONFIG_KEY, DEFAULT_EXCLUDED, exc_info=True)
        raw = None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raw = DEFAULT_EXCLUDED
    return frozenset(c for c in (_canonical(p) for p in raw) if c)


def _is_hosted_nim(base_url: str) -> bool:
    from utils import base_url_host_matches
    return bool(base_url) and base_url_host_matches(base_url, HOSTED_NIM_HOST)


def main_route_excluded(provider: Optional[str], base_url: Optional[str] = "") -> bool:
    """True when ``auto`` side tasks must not use this main route."""
    canonical = _canonical(provider)
    if canonical in {"", "auto"}:
        return False
    excluded = excluded_main_providers()
    if not excluded:
        return False
    url = str(base_url or "").strip()
    if "nvidia" in excluded and _is_hosted_nim(url):
        return True  # any provider id (custom, named custom) pointed at the hosted trial endpoint
    if canonical not in excluded:
        return False
    if canonical == "nvidia" and url and not _is_hosted_nim(url):
        return False  # local / self-hosted NIM is not the trial endpoint
    return True


def log_excluded(task: Optional[str], provider: str) -> None:
    logger.info(
        "Auxiliary %s: main provider %s is listed in auxiliary.%s — side task not sent there "
        "(explicit auxiliary.<task> route or fallback chain only, else off).",
        task or "call", provider, CONFIG_KEY)

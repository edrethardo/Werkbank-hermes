"""``session_id → session_key`` through the live gateway's session store (plan 1.4).

The Werkbank only knows the admin's ``session_id`` (``state.json``). The key is stable, the id
moves with ``/new`` or compression, so the board resolves the id here and subscribes by key.
Only messaging sessions of THIS plugin's profile resolve (same rule as ``werkbank_admin_wake``);
everything else is "unknown".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

_LOCAL_PLATFORMS = {"", "local", "cli", "tui", "api", "api_server", "webhook"}


def profile_of(home: Path) -> str:
    home = Path(home)
    return home.name if home.parent.name == "profiles" else "default"


def _origin_dict(origin: Any) -> Optional[Dict[str, Any]]:
    if origin is None:
        return None
    if isinstance(origin, dict):
        return origin
    to_dict = getattr(origin, "to_dict", None)
    try:
        return to_dict() if callable(to_dict) else None
    except Exception:
        return None


def own_origin(origin: Optional[Dict[str, Any]], profile: str, multiplex: bool) -> Optional[Dict[str, Any]]:
    """*origin* when it is a messaging chat of *profile*, else ``None``."""
    if not isinstance(origin, dict):
        return None
    if str(origin.get("platform") or "").lower() in _LOCAL_PLATFORMS or not origin.get("chat_id"):
        return None
    named = origin.get("profile")
    if named and named != profile:
        return None
    if not named and multiplex and profile != "default":
        return None
    return origin


def _platform_value(entry: Any, origin: Dict[str, Any]) -> Optional[str]:
    platform = getattr(entry, "platform", None)
    value = getattr(platform, "value", None) or (str(platform) if platform else None)
    return value or (str(origin.get("platform")) if origin.get("platform") else None)


def resolve(owner: Any, session_id: str, profile: str) -> Optional[Dict[str, Any]]:
    """``{session_key, session_id, platform, live}`` or ``None``; ``RuntimeError`` without a gateway."""
    if owner is None:
        raise RuntimeError("no live gateway")
    store = getattr(owner, "session_store", None)
    lookup = getattr(store, "lookup_by_session_id", None)
    if lookup is None:
        raise RuntimeError("no session store")
    entry = lookup(session_id)
    if entry is None:
        return None
    multiplex = getattr(getattr(owner, "config", None), "multiplex_profiles", False) is True
    origin = own_origin(_origin_dict(getattr(entry, "origin", None)), profile, multiplex)
    if origin is None:
        return None
    return {"session_key": entry.session_key, "session_id": entry.session_id,
            "platform": _platform_value(entry, origin),
            "live": bool(getattr(owner, "_running", False))}


def current_session_id(owner: Any, session_key: str) -> Optional[str]:
    store = getattr(owner, "session_store", None)
    peek = getattr(store, "peek_session_id", None)
    if peek is None:
        return None
    try:
        return peek(session_key)
    except Exception:
        return None

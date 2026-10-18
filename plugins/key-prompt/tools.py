"""Agent tools: ``request_api_key`` (returns a one-shot link) and ``api_key_request_status``.

Neither tool can carry a key: the schemas have no value field, and the status answer is built
from the request record, which never holds one.
"""

from __future__ import annotations

import json
import time
from typing import Optional

from . import keys, store

SLUG = "schluessel"
_settings = {"public_url": "", "timeout": store.DEFAULT_TIMEOUT}

REQUEST_API_KEY_SCHEMA = {
    "name": "request_api_key",
    "description": (
        "Ask the owner for a provider API key WITHOUT the key passing through this chat. Returns a "
        "one-shot dashboard link (valid ~5 min, usable once). Send that link to the owner, then "
        "call api_key_request_status. Never ask the owner to paste a key into the chat."),
    "parameters": {
        "type": "object",
        "properties": {
            "provider": {"type": "string",
                         "description": "Provider id from Hermes' provider registry, e.g. 'nvidia'."},
            "label": {"type": "string",
                      "description": "Optional label for the credential-pool entry."},
            "purpose": {"type": "string",
                        "description": "One short sentence shown to the owner: what the key is for."},
        },
        "required": ["provider"],
    },
}

API_KEY_REQUEST_STATUS_SCHEMA = {
    "name": "api_key_request_status",
    "description": (
        "Outcome of a request_api_key link: pending, stored, cancelled or expired (plus the "
        "credential label). Never returns the key. wait_seconds (max 300) blocks until the owner "
        "answers or the request expires."),
    "parameters": {
        "type": "object",
        "properties": {
            "request_id": {"type": "string"},
            "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 300},
        },
        "required": ["request_id"],
    },
}


def configure(public_url: str, timeout: int) -> None:
    _settings["public_url"] = str(public_url or "").rstrip("/")
    _settings["timeout"] = int(timeout or store.DEFAULT_TIMEOUT)


def _err(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)


def _session_key(session_id: Optional[str], task_id: Optional[str]) -> str:
    if session_id:
        return str(session_id)
    try:
        from gateway.session_context import get_session_env
        for name in ("HERMES_SESSION_KEY", "HERMES_SESSION_ID"):
            value = get_session_env(name, "")
            if value:
                return str(value)
    except Exception:
        pass
    return str(task_id or "")


def _requester_profile() -> str:
    try:
        from hermes_constants import get_hermes_home, profile_name_for_home
        return profile_name_for_home(get_hermes_home()) or "default"
    except Exception:
        return "default"


def _clean(text, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def request_api_key(args: dict, session_id: Optional[str] = None,
                    task_id: Optional[str] = None, **_kw) -> str:
    provider = keys.normalize_provider((args or {}).get("provider"))
    if provider is None:
        return _err("Unknown or non-API-key provider. Use a provider id from `hermes auth` "
                    "(API-key providers only).")
    if not _settings["public_url"]:
        return _err("key-prompt needs dashboard.public_url (or plugins key-prompt public_url) "
                    "to build the link.")
    session = _session_key(session_id, task_id)
    if not session:
        return _err("No session to bind the request to.")
    try:
        rec = store.create(provider, session=session, label=_clean(args.get("label"), 64),
                           purpose=_clean(args.get("purpose"), 300),
                           requester_profile=_requester_profile(), timeout=_settings["timeout"])
    except store.RateLimited as exc:
        return _err(f"Rate limit: {exc}.")
    except OSError as exc:
        return _err(f"Could not open the request: {exc.__class__.__name__}.")
    expires_in = int(rec["expires"] - rec["created"])
    return json.dumps({
        "link": f"{_settings['public_url']}/p/{SLUG}/?r={rec['id']}",
        "request_id": rec["id"],
        "expires_in": expires_in,
        "instructions": ("Send this link to the owner. The key is typed on that page and stored "
                         "directly; you will only see stored/cancelled/expired via "
                         "api_key_request_status."),
    }, ensure_ascii=False)


def api_key_request_status(args: dict, session_id: Optional[str] = None,
                           task_id: Optional[str] = None, **_kw) -> str:
    rid = str((args or {}).get("request_id") or "").strip()
    rec = store.load(rid)
    if rec is None or rec.get("session") != _session_key(session_id, task_id):
        return _err("No such key request in this session.")
    try:
        wait = max(0, min(int(args.get("wait_seconds") or 0), 300))
    except (TypeError, ValueError):
        wait = 0
    deadline = time.monotonic() + wait
    status = store.effective_status(rec)
    while status == store.PENDING and time.monotonic() < deadline:
        try:
            from tools.interrupt import is_interrupted
            if is_interrupted():
                break
        except Exception:
            pass
        time.sleep(1)
        rec = store.load(rid) or rec
        status = store.effective_status(rec)
    out = {"status": status, "provider": rec.get("provider", ""), "label": rec.get("label", "")}
    if status == store.STORED:
        out["profile"] = rec.get("profile", "default")
        keys.register_stored_value(rec["provider"], out["profile"], rec.get("entry_id", ""))
    return json.dumps(out, ensure_ascii=False)

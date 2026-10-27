"""WB-1256: slash commands from the board chat and the choice cards they open.

* :func:`is_command` — the gateway's own rule (``MessageEvent.get_command``): the text starts with
  ``/`` and its first word (before an optional ``@bot``) contains no further ``/`` — so a path like
  ``/home/…`` stays a message. Such a text goes to the gateway with ``inject_message(…,
  as_command=True)``; it takes the owner's command path there (``gateway/plugin_commands.py``).
* :func:`catalog` — ``GET /v1/commands``: the gateway's slash commands (the registry plus plugin
  commands) for the board's completion, never a hard-coded list.
* :func:`answer_choice` / :func:`pending_choices` — the cards (``choice.request``) of a session.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("plugins.platforms.werkbank")

CHOICE_ID_RE = re.compile(r"ch_[0-9a-f]{16}")


def is_command(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    head = text.lstrip()
    if not head.startswith("/"):
        return False
    first = head.split(maxsplit=1)[0][1:].split("@", 1)[0]
    return bool(first) and "/" not in first


def catalog() -> List[Dict[str, str]]:
    """``[{name, args, description}]`` of the commands a gateway chat accepts, in registry order."""
    try:
        from hermes_cli import commands as registry
    except Exception:
        return []
    out: List[Dict[str, str]] = []
    try:
        gates = registry._resolve_config_gates()
        for cmd in registry.COMMAND_REGISTRY:
            if not registry._is_gateway_available(cmd, gates):
                continue
            out.append({"name": cmd.name, "args": str(cmd.args_hint or ""),
                        "description": str(cmd.describe() or "")})
        known = {c["name"] for c in out}
        for name, description, args in registry._iter_plugin_command_entries():
            if name not in known:
                out.append({"name": name, "args": args, "description": description})
    except Exception:
        logger.info("werkbank: command catalog not readable")
    return out


def pending_choices(session_key: str) -> List[Dict[str, Any]]:
    try:
        from gateway import plugin_commands
    except Exception:
        return []
    return plugin_commands.pending(session_key)


def answer_choice(session_key: str, choice_id: str, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    if not CHOICE_ID_RE.fullmatch(choice_id or ""):
        return 400, {"error": "bad id", "grund": "Ungültige Auswahl-ID."}
    try:
        from gateway import plugin_commands
    except Exception:
        return 501, {"error": "not implemented", "grund": "Dieses Hermes kennt keine Auswahlkarten (WB-1256)."}
    return plugin_commands.answer(session_key, choice_id, body.get("index"), decided_by="werkbank")

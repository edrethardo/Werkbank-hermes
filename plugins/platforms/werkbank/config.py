"""``plugins.entries.werkbank`` of the plugin's profile ``config.yaml``.

    plugins:
      entries:
        werkbank:
          enabled: true              # default false: no port is opened without it
          port: 8767                 # default 8767 (NOT 8765, the board's port)
          mirror_to_telegram: true   # default true: the board text, as the owner's message, in the topic
          mirror_name: Aaron         # recommended; unset: "Du" (never the Telegram @-username)

The Werkbank board reads the same ``port`` (``bruecke.read_port``). Keep it a plain number.

Sending and stopping need a SECOND entry, the core's gate under the plugin's id (a bundled
platform plugin's id is its path, ``platforms/werkbank``); this module does not read it::

    plugins:
      entries:
        platforms/werkbank:
          allow_gateway_injection: true
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping

ENTRY = "werkbank"
DEFAULT_PORT = 8767
BOARD_PORT = 8765


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    port: int = DEFAULT_PORT
    mirror_to_telegram: bool = True
    mirror_name: str = ""
    problem: str = ""


def _load(home: Path) -> Mapping[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.plugins_loader import _plugin_home_scope
        with _plugin_home_scope(Path(home)):
            return load_config_readonly() or {}
    except Exception:
        pass
    try:
        from utils import fast_safe_load
        data = fast_safe_load((Path(home) / "config.yaml").read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "yes", "on", "1"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("false", "no", "off", "0"):
        return False
    return default


def from_entry(entry: Any) -> Settings:
    if not isinstance(entry, Mapping):
        return Settings()
    problem = ""
    port = entry.get("port", DEFAULT_PORT)
    if isinstance(port, str) and port.strip().isdigit():
        port = int(port.strip())
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        problem, port = "port is not a port number", DEFAULT_PORT
    elif port == BOARD_PORT:
        problem = "port 8765 is the Werkbank board's own port"
    name = entry.get("mirror_name")
    return Settings(enabled=_bool(entry.get("enabled"), False), port=port,
                    mirror_to_telegram=_bool(entry.get("mirror_to_telegram"), True),
                    mirror_name=name.strip() if isinstance(name, str) else "", problem=problem)


def read(home: Path) -> Settings:
    cfg: Dict[str, Any] = dict(_load(home))
    entries = ((cfg.get("plugins") or {}) if isinstance(cfg.get("plugins"), Mapping) else {}).get("entries")
    return from_entry(entries.get(ENTRY) if isinstance(entries, Mapping) else None)

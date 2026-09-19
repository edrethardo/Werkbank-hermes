"""Welches Bildprotokoll waehlt eine FRISCHE Dashboard-Chat-PTY?

Baut die Umgebung exakt wie ``_resolve_chat_argv`` und fragt den gebauten
``entry.js``-Code, was ``IMAGE_PROTOCOL`` daraus macht — ohne auf eine
Modellantwort zu warten (die Chat-Sonde brauchte dafuer >240 s und lieferte
darum nie ein Ergebnis).

    .venv/bin/python scripts/e2e/chat_pty_protocol.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Dieselbe Aufloesung wie ui-tui/src/config/env.ts, als Frage an Node gestellt.
SNIPPET = """
const truthy = v => /^(?:1|true|yes|on)$/i.test((v ?? '').trim())
const DASHBOARD_TUI_MODE = truthy(process.env.HERMES_TUI_DASHBOARD)
const override = (process.env.HERMES_TUI_IMAGE_PROTOCOL ?? '').trim().toLowerCase()
const IMAGE_PROTOCOL = DASHBOARD_TUI_MODE
  ? 'iterm'
  : ['kitty', 'iterm', 'none'].includes(override) ? override : ''
console.log(JSON.stringify({
  dashboard: DASHBOARD_TUI_MODE,
  inherited: process.env.HERMES_TUI_IMAGE_PROTOCOL ?? null,
  resolved: IMAGE_PROTOCOL,
}))
"""


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from hermes_cli.web_server_chat import _resolve_chat_argv

    _argv, _cwd, env = _resolve_chat_argv(
        resume=None, program=None, project=None, profile=None,
        provider=None, model=None, chatgpt_mode=None,
    )
    env = dict(env or os.environ)

    out = subprocess.run(["node", "-e", SNIPPET], env=env,
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        print(out.stderr)
        return 1

    data = json.loads(out.stdout)
    print(f"HERMES_TUI_DASHBOARD gesetzt : {data['dashboard']}")
    print(f"geerbtes IMAGE_PROTOCOL      : {data['inherited']!r}")
    print(f"gewaehlt                     : {data['resolved']!r}")

    ok = data["resolved"] == "iterm"
    print("\n" + ("OK — die Chat-PTY waehlt iterm." if ok
                  else "FEHLER — die Chat-PTY waehlt nicht iterm."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

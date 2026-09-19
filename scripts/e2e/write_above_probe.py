"""Greift ``writeAbove()`` in einer Dashboard-artigen PTY?

Die Chat-Sonde (``dashboard_chat_image.py``) wartete auf eine Modellantwort und
bekam in 240 s keine — sie konnte ueber den Bildweg darum nichts aussagen. Diese
Sonde schneidet das Modell ab: ein Mini-Ink-Programm ruft ``writeAbove`` mit einer
Markierung, unter denselben Umgebungsvariablen, die der Dashboard-PTY-Spawner
setzt (``HERMES_TUI_DASHBOARD``/``INLINE``/``DISABLE_MOUSE``, ``TERM=xterm-256color``).

Kommt die Markierung aus der PTY, lebt der Auslieferungsweg und der Fehler sitzt
davor (RPC/Protokollwahl). Kommt sie nicht, bricht ``writeAbove`` still ab.

    .venv/bin/python scripts/e2e/write_above_probe.py
"""
from __future__ import annotations

import os
import select
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARKER = "<<<WRITE_ABOVE_MARKER>>>"
BUNDLE = ROOT / "ui-tui" / "write_above_probe.mjs"


def main() -> int:
    if not BUNDLE.is_file():
        print(f"Sonde fehlt: {BUNDLE}")
        return 2

    import ptyprocess

    env = dict(os.environ)
    for key in list(env):
        if key.startswith("HERMES_SESSION") or key in {
                "HERMES_TUI_RESUME", "HERMES_TUI_SIDECAR_URL", "HERMES_TUI_IMAGE_PROTOCOL"}:
            env.pop(key, None)
    # Genau das, was hermes_cli/web_server_chat.py der Chat-PTY mitgibt.
    env["HERMES_TUI_DASHBOARD"] = "1"
    env["HERMES_TUI_INLINE"] = "1"
    env["HERMES_TUI_DISABLE_MOUSE"] = "1"
    env["TERM"] = "xterm-256color"

    child = ptyprocess.PtyProcessUnicode.spawn(
        ["node", str(BUNDLE)], cwd=str(ROOT / "ui-tui"), env=env, dimensions=(30, 100))

    fd = child.fd
    chunks: list[str] = []
    end = time.time() + 15

    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if not r:
            continue
        try:
            data = os.read(fd, 65536)
        except OSError:
            break
        if not data:
            break
        chunks.append(data.decode("utf-8", "replace"))

    whole = "".join(chunks)
    found = MARKER in whole

    print(f"Bytes empfangen : {len(whole)}")
    print(f"Markierung da   : {found}")
    print(f"'frame' gemalt  : {'frame' in whole}")

    if not found:
        print("\nRoh (letzte 600 Zeichen):")
        print(repr(whole[-600:]))

    try:
        child.terminate(force=True)
    except Exception:
        pass

    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main())

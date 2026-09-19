"""Antwortet ``image.terminal_sequence`` im LAUFENDEN Gateway mit einer Sequenz?

Die Handler-Funktion allein zu testen genuegt nicht: ``methods_images`` wird per
``bind_module`` in server.py neu gebunden, und die RPC muss ueber die echte
stdio-Schleife erreichbar sein. Diese Sonde spricht den Gateway so an, wie der
Ink-Renderer es tut.

    .venv/bin/python scripts/e2e/image_rpc_probe.py [pfad]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMAGE = sys.argv[1] if len(sys.argv) > 1 else "/tmp/kitty_test.png"


def main() -> int:
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("HERMES_SESSION") or key in {
                "HERMES_TUI_RESUME", "HERMES_TUI_SIDECAR_URL", "HERMES_TUI_IMAGE_PROTOCOL"}:
            env.pop(key, None)

    proc = subprocess.Popen(
        [sys.executable, "-m", "tui_gateway.entry"],
        cwd=str(ROOT), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )

    def call(rid: int, method: str, params: dict) -> dict | None:
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                                     "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        end = time.time() + 45
        while time.time() < end:
            line = proc.stdout.readline()
            if not line:
                return None
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == rid:
                return msg
        return None

    results = {}
    for label, params in (
            ("ohne protocol", {"path": IMAGE}),
            ("protocol=iterm", {"path": IMAGE, "protocol": "iterm"}),
            ("protocol=kitty", {"path": IMAGE, "protocol": "kitty"}),
    ):
        msg = call(len(results) + 1, "image.terminal_sequence", params)
        if msg is None:
            results[label] = "KEINE ANTWORT"
            continue
        if "error" in msg:
            results[label] = f"FEHLER {msg['error']}"
            continue
        r = msg.get("result", {})
        seq = r.get("sequence") or ""
        results[label] = (f"available={r.get('available')} protocol={r.get('protocol')!r} "
                          f"cols={r.get('cols')} rows={r.get('rows')} seq={len(seq)}B")

    print(f"Bild: {IMAGE}\n")
    for label, value in results.items():
        print(f"  {label:16} -> {value}")

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    return 0


if __name__ == "__main__":
    sys.exit(main())

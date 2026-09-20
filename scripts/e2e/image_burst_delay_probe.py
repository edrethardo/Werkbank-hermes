"""Wie lange muss die Pause zwischen ``/image <pfad>`` und ``\\r`` mindestens sein?

Ergaenzt image_burst_probe.py: dort ist gemessen, DASS ein Burst das Return
verschluckt. Hier wird die noetige Pause bestimmt, damit im Plugin kein Wert
geraten werden muss.

Pro Lauf eine frische TUI, eine Pause, dann die Frage: steht nach dem Return ein
``[[ Image N ]]`` im Composer (Befehl ausgefuehrt) oder noch ``/image`` als Text?

    .venv/bin/python scripts/e2e/image_burst_delay_probe.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from image_burst_probe import IMG, TOKEN_RE, drain, plain, run_case  # noqa: E402

import os  # noqa: E402
import pty  # noqa: E402
import signal  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402

from image_burst_probe import ROOT, disable_echo, set_winsize, wait_for_ready  # noqa: E402

DELAYS_MS = (0, 30, 60, 120, 250, 500)


def run_delay(delay_ms: int) -> dict:
    master, slave = pty.openpty()
    disable_echo(slave)
    set_winsize(slave, 30, 100)
    env = {k: v for k, v in os.environ.items()
           if k not in ("HERMES_TUI_RESUME", "HERMES_SESSION_ID", "HERMES_TUI_SIDECAR_URL")}
    env["TERM"] = "xterm-256color"
    env["COLUMNS"], env["LINES"] = "100", "30"

    proc = subprocess.Popen(
        [str(ROOT / ".venv/bin/hermes"), "-p", "hermes_dev", "--tui"],
        stdin=slave, stdout=slave, stderr=slave, env=env, cwd=str(ROOT),
        preexec_fn=os.setsid,
    )
    os.close(slave)
    try:
        _boot, ready = wait_for_ready(master, 45.0)
        os.write(master, f"/image {IMG}".encode())
        if delay_ms:
            time.sleep(delay_ms / 1000)
        os.write(master, b"\r")
        screen = plain(drain(master, 12.0))
        return {
            "delay_ms": delay_ms,
            "ready": ready,
            "token": len(TOKEN_RE.findall(screen)),
            "befehl_steht_noch": "/image" in screen.split("[[")[0][-200:] if "[[" in screen else "/image" in screen,
        }
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        os.close(master)


def main() -> int:
    if not IMG.exists():
        print(f"Testbild fehlt: {IMG}")
        return 2
    print("Pause  bereit  [[Image]]  Befehl ausgefuehrt")
    for ms in DELAYS_MS:
        r = run_delay(ms)
        ok = r["token"] >= 1
        print(f"{ms:5d}  {str(r['ready']):6}  {r['token']:9d}  {'JA' if ok else 'NEIN'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

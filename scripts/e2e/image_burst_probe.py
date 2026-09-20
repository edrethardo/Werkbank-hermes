"""Was macht die TUI aus einem Burst ``"/image <pfad>\\r"`` in EINEM Write?

Reproduktion des gemeldeten Fehlers ("Bild angehaengt, Nachricht laesst sich nicht
abschicken", am Ende mehrfach dasselbe Bild im Prompt) ohne Browser, ohne Dashboard
und ohne Modellaufruf: eine echte TUI in einer echten PTY, der Burst exakt so
geschrieben, wie ``sendInput(leaf, `/image ${path}\\r`)`` im i3-Plugin es tut.

WICHTIG: das PTY-Echo wird abgeschaltet (termios), sonst misst die Sonde ihre
eigenen Tastendruecke und nicht Inks Bildschirm.

Gemessen wird der Bildschirm nach dem Burst:
  - ``[[ Image N ]]``   -> Anhang hat funktioniert
  - ``/image`` steht noch als Text -> Return kam nicht an (der gemeldete Zustand)

Vergleichslauf: derselbe Text, aber Text und ``\\r`` in ZWEI Writes mit Pause.

    .venv/bin/python scripts/e2e/image_burst_probe.py
"""
from __future__ import annotations

import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import fcntl
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMG = Path("/tmp/kitty_test.png")

TOKEN_RE = re.compile(r"\[\[\s*Image\s+\d+\s*\]\]")
ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-B]|\x1b[=>]")


def disable_echo(fd: int) -> None:
    attrs = termios.tcgetattr(fd)
    attrs[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON)
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


def set_winsize(fd: int, rows: int, cols: int) -> None:
    """Ohne TIOCSWINSZ meldet die PTY 0 Spalten und Ink bricht nach jedem Zeichen um."""
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def drain(master: int, budget: float) -> bytes:
    out = bytearray()
    end = time.time() + budget
    while time.time() < end:
        r, _, _ = select.select([master], [], [], 0.2)
        if not r:
            continue
        try:
            chunk = os.read(master, 65536)
        except OSError:
            break
        if not chunk:
            break
        out.extend(chunk)
    return bytes(out)


def wait_for_ready(master: int, budget: float) -> tuple[bytes, bool]:
    """Warte, bis Ink den Composer gezeichnet hat (Prompt-Pfeil), nicht blind schlafen."""
    out = bytearray()
    end = time.time() + budget
    while time.time() < end:
        r, _, _ = select.select([master], [], [], 0.3)
        if r:
            try:
                out.extend(os.read(master, 65536))
            except OSError:
                break
        text = plain(bytes(out))
        if ">" in text and ("Hermes" in text or "hermes" in text or "?" in text):
            time.sleep(2.0)
            r, _, _ = select.select([master], [], [], 0.5)
            if r:
                out.extend(os.read(master, 65536))
            return bytes(out), True
    return bytes(out), False


def plain(raw: bytes) -> str:
    return ANSI_RE.sub(b"", raw).decode("utf-8", "replace")


def run_case(label: str, split_return: bool) -> dict:
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
        boot, ready = wait_for_ready(master, 45.0)
        payload = f"/image {IMG}"
        if split_return:
            os.write(master, payload.encode())
            time.sleep(1.5)
            os.write(master, b"\r")
        else:
            os.write(master, f"{payload}\r".encode())

        after = drain(master, 12.0)
        first = plain(after)

        # Der gemeldete Folgezustand: der Nutzer drueckt Return, weil nichts passiert.
        os.write(master, b"\r")
        second_raw = drain(master, 10.0)
        second = plain(second_raw)

        return {
            "label": label,
            "ready": ready,
            "token_nach_burst": len(TOKEN_RE.findall(first)),
            "image_text_steht": "/image" in first,
            "token_nach_extra_return": len(TOKEN_RE.findall(second)),
            "tail": second[-500:] if second.strip() else first[-500:],
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

    for label, split in (("EIN Write (wie das i3-Plugin)", False),
                         ("ZWEI Writes mit Pause (wie der Dashboard-Chat)", True)):
        r = run_case(label, split)
        print(f"\n=== {r['label']} ===")
        print(f"Composer bereit             : {r['ready']}")
        print(f"[[ Image N ]] nach Burst    : {r['token_nach_burst']}")
        print(f"'/image' steht noch im Bild : {r['image_text_steht']}")
        print(f"[[ Image N ]] nach Return   : {r['token_nach_extra_return']}")
        print("--- letzte Zeilen ---")
        print(r["tail"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

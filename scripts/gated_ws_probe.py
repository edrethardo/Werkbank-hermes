"""Gated mode: does the i3 page's WS actually authenticate?

The bug this pins: with a password on the dashboard, a browser cannot send a header on a
WS upgrade, and the session COOKIE is not accepted either — the core wants a single-use
`?ticket=` minted via POST /api/auth/ws-ticket. app.js connected without one, so every
pane socket was rejected while the HTML rendered fine (cookie auth covers plain GETs).
Symptom: "nur ne leere shell".

Run:
    HERMES_HOME=<temp> WB618_PLUGIN_DIR=<plugin> .venv/bin/python scripts/gated_ws_probe.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'OK  ' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAIL.append(name)


def main() -> int:
    plugin_src = Path(os.environ["WB618_PLUGIN_DIR"])
    home = Path(os.environ["HERMES_HOME"])
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    link = home / "plugins" / "hermes-i3"
    if not link.exists():
        link.symlink_to(plugin_src)
    (home / "config.yaml").write_text("plugins:\n  enabled:\n    - hermes-i3\n", encoding="utf-8")

    from starlette.testclient import TestClient

    from hermes_cli.plugins import discover_plugins
    import hermes_cli.web_server as ws

    discover_plugins(force=True)

    ws.app.state.bound_host = ""
    # THE point of this probe: gated, exactly like a dashboard with a password.
    ws.app.state.auth_required = True
    client = TestClient(ws.app)

    # A real browser session is an OAuth cookie pair we cannot forge here. What the bug was
    # about is the TICKET path, so mint one the way the endpoint does and drive the socket
    # with it — that is the credential app.js now sends.
    from hermes_cli.dashboard_auth.ws_tickets import mint_ticket

    ticket = mint_ticket(user_id="probe", provider="local")
    check("ticket minted", bool(ticket), f"len={len(ticket)}")

    # 1. The shipped app.js must mint a ticket — the actual fix.
    #    Served unauthenticated here only because the gate is what we are testing below;
    #    the asset route itself is covered by the e2e script.
    src = (plugin_src / "static" / "app.js").read_text(encoding="utf-8")
    check("app.js requests a ws-ticket", "/api/auth/ws-ticket" in src)
    check("app.js sends ?ticket= on the socket", "?ticket=" in src)
    check("app.js honours __HERMES_AUTH_REQUIRED__", "__HERMES_AUTH_REQUIRED__" in src)

    # 2. A socket WITHOUT a ticket must be refused — the old, broken behaviour.
    refused = False
    try:
        with client.websocket_connect("/p/i3/ws") as sock:
            sock.send_text(json.dumps({"type": "open", "cols": 80, "rows": 24}))
            sock.receive()
    except Exception:  # noqa: BLE001 - a rejected upgrade is the pass condition
        refused = True
    check("socket without ticket is refused", refused)

    # 6. A socket WITH the minted ticket must open a real pane.
    pane_id, output = None, b""
    if ticket:
        try:
            with client.websocket_connect(f"/p/i3/ws?ticket={ticket}") as sock:
                sock.send_text(json.dumps({"type": "open", "cols": 100, "rows": 30}))
                deadline = time.time() + 120
                while time.time() < deadline:
                    msg = sock.receive()
                    if msg.get("type") == "websocket.disconnect":
                        break
                    if msg.get("text"):
                        frame = json.loads(msg["text"])
                        if frame.get("type") == "opened":
                            pane_id = frame["pane"]
                        elif frame.get("type") in ("error", "exit"):
                            print("   pane frame:", frame)
                            break
                    elif msg.get("bytes"):
                        output += msg["bytes"]
                        if len(output) > 800:
                            break
                if pane_id:
                    sock.send_text(json.dumps({"type": "kill"}))
        except Exception as exc:  # noqa: BLE001
            check("socket with ticket", False, repr(exc))
        else:
            check("socket with ticket opened a pane", bool(pane_id), f"pane={pane_id}")
            check("pane produced output", len(output) > 200, f"{len(output)} bytes")

    # 7. Tickets are single-use: the same one must not work twice.
    if ticket and pane_id:
        reused = False
        try:
            with client.websocket_connect(f"/p/i3/ws?ticket={ticket}") as sock:
                sock.send_text(json.dumps({"type": "open", "cols": 80, "rows": 24}))
                sock.receive()
                reused = True
        except Exception:  # noqa: BLE001
            reused = False
        check("ticket is single-use", not reused)

    for mod in list(sys.modules.values()):
        inst = getattr(mod, "_page", None) if mod else None
        if inst is not None and hasattr(inst, "panes"):
            inst.shutdown()

    print()
    if FAIL:
        print(f"{len(FAIL)} check(s) failed: {', '.join(FAIL)}")
        return 1
    print("Gated WS auth: all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

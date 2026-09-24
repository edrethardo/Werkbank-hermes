"""End-to-end: a background command's progress bar is live in the embedded /chat — and never in the transcript.

WB-777. Everything real except the model: a throwaway ``HERMES_HOME``, the real dashboard app
(``hermes_cli.web_server``) served by uvicorn, the real ``/api/pty`` WebSocket the /chat page opens
(ONE socket = one PTY writer), the real ``hermes --tui`` child with its real ``tui_gateway``, the real
``terminal`` tool spawning a real background process, the real session database. The model is a
local OpenAI-compatible stub that scripts two turns: call ``terminal(background=true)`` on a
progress script, then say "started".

The progress script redraws ONE line with ``\\r`` every 0.4 s for ~12 s (``copy NN% frame-NN``),
then prints ``copy 100% settled``. The screen side (``bg_progress_dock_client.mjs``) feeds the PTY
bytes into a headless xterm.js at iPhone-ish geometry and samples the visible screen.

Checks (the ticket's acceptance):
  1. several DIFFERENT progress frames were visible on screen while the process ran,
     without the user starting anything;
  2. that happened through the single /api/pty socket;
  3. afterwards, NO row of the session database carries a progress frame.

Run (≈60 s; needs the built ui-tui/dist and hermes_cli/web_dist):

    .venv/bin/python scripts/e2e/bg_progress_dock.py
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COLS, ROWS = 46, 30  # an iPhone portrait /chat pane
FAILURES: list[str] = []

PROGRESS = (
    "import sys, time\n"
    "for i in range(30):\n"
    "    sys.stdout.write(f'\\rcopy {i * 3:3d}% frame-{i:02d}'); sys.stdout.flush(); time.sleep(0.4)\n"
    "sys.stdout.write('\\rcopy 100% settled\\n'); sys.stdout.flush()\n"
)


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'OK  ' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILURES.append(name)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _StubModel(BaseHTTPRequestHandler):
    """OpenAI chat-completions stub: 1st turn → terminal(background) tool call, then plain text."""

    command = ""
    requests: list = []

    def log_message(self, *_a):
        pass

    def do_GET(self):  # /v1/models probes
        self._json({"object": "list", "data": [{"id": "stub-model", "object": "model"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        _StubModel.requests.append(body)
        msgs = body.get("messages") or []
        has_tool_result = any(m.get("role") == "tool" for m in msgs)
        if not any(t.get("function", {}).get("name") == "terminal" for t in body.get("tools") or []):
            # auxiliary calls (titles etc.): answer plainly
            return self._reply(body, {"role": "assistant", "content": "Progress test"})
        if has_tool_result:
            return self._reply(body, {"role": "assistant", "content": "started"})
        args = {"command": _StubModel.command, "background": True, "notify": False}
        return self._reply(body, {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_progress", "type": "function",
            "function": {"name": "terminal", "arguments": json.dumps(args)}}]})

    def _reply(self, body, message):
        finish = "tool_calls" if message.get("tool_calls") else "stop"
        if body.get("stream"):
            delta = dict(message)
            if delta.get("tool_calls"):
                delta["tool_calls"] = [{"index": 0, **tc} for tc in delta["tool_calls"]]
            chunks = [
                {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "stub-model",
                 "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "stub-model",
                 "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
            ]
            data = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._json({"id": "c1", "object": "chat.completion", "created": 1, "model": "stub-model",
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    # HOME too, not only HERMES_HOME: the /chat SPA asks for `profile=default`, which the dashboard
    # resolves from `$HOME/.hermes` — with the real HOME that turn lands in the user's own profile.
    fake_home = Path(tempfile.mkdtemp(prefix="wb777-home-"))
    home = fake_home / ".hermes"
    home.mkdir()
    work = Path(tempfile.mkdtemp(prefix="wb777-work-"))
    script = work / "progress.py"
    script.write_text(PROGRESS, encoding="utf-8")
    _StubModel.command = f"{sys.executable} {script}"

    model_port = _free_port()
    model_srv = ThreadingHTTPServer(("127.0.0.1", model_port), _StubModel)
    threading.Thread(target=model_srv.serve_forever, daemon=True).start()

    (home / "config.yaml").write_text(
        "model:\n"
        "  default: stub-model\n"
        "  provider: custom\n"
        f"  base_url: http://127.0.0.1:{model_port}/v1\n"
        "  api_key: stub\n"
        "approvals:\n  mode: \"off\"\n"
        "terminal:\n  backend: local\n"
        f"  cwd: {work}\n",
        encoding="utf-8")

    dash_port = _free_port()
    token = "wb777-e2e-token"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HERMES_SESSION", "HERMES_TUI_", "HERMES_UI_"))}
    env.update(HOME=str(fake_home), HERMES_HOME=str(home), HERMES_DASHBOARD_SESSION_TOKEN=token,
               PYTHONUNBUFFERED="1", TERMINAL_CWD=str(work))
    dash_log = open(work / "dashboard.log", "w")
    dash = subprocess.Popen(
        [sys.executable, str(REPO / "hermes"), "dashboard", "--isolated", "--host", "127.0.0.1",
         "--port", str(dash_port), "--no-open", "--skip-build"],
        cwd=str(REPO), env=env, stdout=dash_log, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", dash_port), timeout=1):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            check("dashboard came up", False, (work / "dashboard.log").read_text()[-2000:])
            return 1
        time.sleep(1.5)

        url = f"ws://127.0.0.1:{dash_port}/api/pty?token={token}&channel=wb777-e2e"
        client = subprocess.run(
            ["node", str(REPO / "scripts" / "e2e" / "bg_progress_dock_client.mjs"), url, str(COLS), str(ROWS),
             "start the copy", "45"],
            cwd=str(REPO), capture_output=True, text=True, timeout=120)
        samples = [json.loads(l) for l in client.stdout.splitlines() if l.startswith("{")]
        (work / "screens.jsonl").write_text(client.stdout, encoding="utf-8")
        screens = [s for s in samples if "screen" in s]
        check("one /api/pty socket delivered a TUI", any(s["bytes"] > 0 for s in screens),
              client.stderr[-400:])
        check("prompt was typed into the composer", any(s.get("typed") for s in screens))

        seen_frames: list[str] = []
        for s in screens:
            for line in s["screen"]:
                for m in re.findall(r"frame-(\d\d)", line):
                    if m not in seen_frames:
                        seen_frames.append(m)
        check("several distinct progress frames were on screen while it ran", len(seen_frames) >= 5,
              f"frames seen: {seen_frames}")
        live_rows = [line for s in screens for line in s["screen"] if "frame-" in line]
        check("frames painted on the dock's live row", any("↳" in l for l in live_rows),
              (live_rows[-1] if live_rows else "none"))
        final = screens[-1]["screen"] if screens else []
        print("--- final screen ---\n" + "\n".join(final) + "\n--------------------", flush=True)

        if os.environ.get("WB777_WEBKIT"):
            # The real /chat SPA in WebKit with iPhone emulation, as a SECOND session: it runs its
            # own turn (the stub answers every fresh prompt with the same tool call).
            web = subprocess.run(
                ["node", str(REPO / "scripts" / "e2e" / "bg_progress_dock_webkit.mjs"),
                 f"http://127.0.0.1:{dash_port}", "start the copy", "22", str(work)],
                cwd=str(REPO), capture_output=True, text=True, timeout=180)
            events = [json.loads(l) for l in web.stdout.splitlines() if l.startswith("{")]
            done = next((e for e in events if e.get("event") == "done"), {})
            check("iPhone /chat opened exactly ONE /api/pty socket", len(done.get("ptySockets") or []) == 1,
                  f"{done.get('ptySockets')} {web.stderr[-300:]}")
            print("webkit screenshots: " + ", ".join(e["path"] for e in events if e.get("event") == "shot"),
                  flush=True)

        # --- the session database: what the conversation actually persisted ---
        db = sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True)
        rows = db.execute("select role, coalesce(content,''), coalesce(tool_calls,''), coalesce(api_content,'') "
                          "from messages").fetchall()
        db.close()
        check("the turn persisted (user + tool call + tool result + reply)",
              {r[0] for r in rows} >= {"user", "assistant", "tool"}, f"roles={[r[0] for r in rows]}")
        leaked = [r for r in rows if "frame-" in "".join(r)]
        check("no session-db row carries a progress frame", not leaked, repr(leaked)[:600])
        model_saw = [json.dumps(b) for b in _StubModel.requests]
        check("no request to the model carried a progress frame", not any("frame-" in b for b in model_saw))
        print(f"db rows: {[(r[0], r[1][:60]) for r in rows]}", flush=True)
    finally:
        dash.terminate()
        try:
            dash.wait(timeout=15)
        except subprocess.TimeoutExpired:
            dash.kill()
        model_srv.shutdown()
        dash_log.close()
        print(f"artifacts: {work}  home: {home}", flush=True)

    print("PASS" if not FAILURES else f"FAILED: {FAILURES}", flush=True)
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())

"""Behaviour contract: a background process's progress output reaches the display, not the model.

WB-777. A long copy (rsync, SD card, build) redraws one progress line with ``\\r`` many times a
second. The TUI must be able to show that line live — the gateway streams every chunk as
``agent.terminal.output`` to the session that owns the process — while the model's context
(the completion/poll/log output it reads, which the transcript then stores) carries the settled
state, never the stream of frames. Progress as messages would break the prompt cache every tick
and read as a wall of stale percentages.

A later simplification must not drop either half: the display stream (the dock goes blind again)
or the collapse (the frames flood the context again).
"""

import sys
import time

from tools.process_registry import _completion_output, process_registry
from tui_gateway import server

FRAMES = 12
# Python child, unbuffered: one ``\r`` frame per tick, then the settled line — the shape of rsync --progress.
PROGRESS_SCRIPT = (
    "import sys, time\n"
    f"for i in range({FRAMES}):\n"
    "    sys.stdout.write(f'\\rcopy {i * 5:3d}% frame-{i:02d}'); sys.stdout.flush(); time.sleep(0.05)\n"
    "sys.stdout.write('\\rcopy 100% settled\\n'); sys.stdout.flush()\n"
)


def test_progress_frames_reach_the_display_stream_but_not_the_model_context(monkeypatch, tmp_path):
    script = tmp_path / "progress.py"
    script.write_text(PROGRESS_SCRIPT)
    server._sessions["progress-sid"] = {"session_key": "progress-key", "history": []}
    emitted: list = []
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None: emitted.append((event, sid, payload)) or True)
    monkeypatch.setattr(process_registry, "on_output", None)
    monkeypatch.setattr(process_registry, "on_close", None)
    try:
        server._wire_desktop_sinks()  # what every TUI session's notification poller does at start
        proc = process_registry.spawn_local(
            f"{sys.executable} {script}", cwd=str(tmp_path), task_id="progress-task", session_key="progress-key")
        deadline = time.time() + 20
        while not proc.exited and time.time() < deadline:
            time.sleep(0.05)
        assert proc.exited, "progress fixture did not finish"
        time.sleep(0.3)  # reader drains the tail after exit

        # Display: every frame streamed, addressed to the owning TUI session.
        stream = [p for e, sid, p in emitted if e == "agent.terminal.output" and sid == "progress-sid"]
        streamed = "".join(p["chunk"] for p in stream if p["process_id"] == proc.id)
        for i in range(FRAMES):
            assert f"frame-{i:02d}" in streamed
        assert "settled" in streamed

        # Model: every surface it reads carries the settled line and none of the redraw frames.
        model_views = {
            "completion": _completion_output(proc)["output"],
            "poll": process_registry.poll(proc.id)["output_preview"],
            "log": process_registry.read_log(proc.id)["output"],
        }
        for name, text in model_views.items():
            assert "copy 100% settled" in text, name
            assert "frame-" not in text, f"{name} leaked progress frames into the model context: {text!r}"

        # And the stream itself never became conversation: no message event rode along with it.
        assert not [e for e, _sid, _p in emitted if e.startswith("message.")]
    finally:
        server._sessions.pop("progress-sid", None)

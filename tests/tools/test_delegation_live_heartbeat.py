"""WB-1332: the live transcript is the child's sign of life for the Werkbank.

The board reads ``cache/delegation/live/<deleg>/task-<n>.log`` and treats a
stale file as a dead child. A child in one long model call or one long tool
call writes nothing for many minutes, so ``heartbeat`` touches the file while
the child's run is on the stack — and stops with it. After an OOM kill or a
gateway restart nothing touches it any more; the staleness is the signal.
"""

import os
import threading
import time
from dataclasses import fields

from tools import delegation_live_log as dll
from tools.delegate_tool_dispatch import _Batch
from tools.delegation_live_log import LiveTranscriptWriter, heartbeat


def _alt(path, secs=3600):
    t = time.time() - secs
    os.utime(path, (t, t))


def test_heartbeat_touches_the_log_while_the_body_runs():
    w = LiveTranscriptWriter("deleg_hb1", 0, "WB-1: x")
    _alt(w.path)
    with heartbeat(w, interval=0.05):
        time.sleep(0.3)
        assert time.time() - w.path.stat().st_mtime < 5


def test_heartbeat_stops_with_the_body():
    w = LiveTranscriptWriter("deleg_hb2", 0, "WB-1: x")
    with heartbeat(w, interval=0.05):
        pass
    _alt(w.path)
    time.sleep(0.3)
    assert time.time() - w.path.stat().st_mtime > 3000
    assert not [t for t in threading.enumerate() if t.name.startswith("live-heartbeat-deleg_hb2")]


def test_heartbeat_without_writer_is_a_no_op():
    with heartbeat(None, interval=0.05):
        pass


def test_heartbeat_never_raises_when_the_log_is_gone():
    w = LiveTranscriptWriter("deleg_hb3", 0, "WB-1: x")
    w.path.unlink()
    with heartbeat(w, interval=0.02):
        time.sleep(0.1)


def test_run_child_beats_while_the_child_runs(monkeypatch):
    w = LiveTranscriptWriter("deleg_hb4", 0, "WB-1: x")
    _alt(w.path)
    monkeypatch.setattr(dll, "HEARTBEAT_SECONDS", 0.05)
    seen = {}

    def fake_run_single_child(**kw):
        time.sleep(0.3)
        seen["age"] = time.time() - w.path.stat().st_mtime
        return {"task_index": kw["task_index"], "status": "completed"}

    import tools.delegate_tool as dt
    monkeypatch.setattr(dt, "_run_single_child", fake_run_single_child)
    batch = _Batch(**{f.name: None for f in fields(_Batch)})
    batch.live_writers = [w]
    batch.parent_agent = None
    child = object()
    out = batch.run_child(0, {"goal": "WB-1: x"}, child)
    assert out["status"] == "completed"
    assert seen["age"] < 5

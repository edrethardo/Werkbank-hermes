"""WB-1338: the board writes to a running subagent of an admin chat (``steer``).

A subagent (``delegate_task``) is no chat of its own: its history is the live transcript
(``cache/delegation/live/<delegation_id>/task-<n>.log``), and the way to reach it is the
parent's steer path — ``tools.delegate_tool_registry.steer_subagent``, the text lands at the
child's next iteration boundary (the running tool call is never cut).

The board names the child the way it knows it — delegation id + task index (the transcript),
never a path and never a registry id it had to guess. The live record must

* belong to the delegation (``delegation_id``) and to that task (its transcript file name),
* be owned by THIS session — the chat the board resolved (``owner_agent_session_id`` or the
  gateway ``owner_session_id``),
* still accept steering — else 409 „Subagent beendet“.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, Tuple

DELEG_RE = re.compile(r"deleg_[0-9a-f]{6,32}")
MAX_STEER_TEXT = 20_000


def _records():
    from tools import delegate_tool_registry as reg
    with reg._active_subagents_lock:
        return [(sid, dict(r)) for sid, r in reg._active_subagents.items()]


def _task_of(record: Dict[str, Any]):
    path = getattr(record.get("agent"), "_live_transcript_path", None)
    if not isinstance(path, str):
        return None
    m = re.fullmatch(r"task-(\d{1,4})\.log", os.path.basename(path))
    return int(m.group(1)) if m else None


def steer(session_ids: Iterable[str], body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    deleg, task, text = body.get("delegation_id"), body.get("task_index"), body.get("text")
    if not isinstance(deleg, str) or not DELEG_RE.fullmatch(deleg):
        return 400, {"error": "bad delegation_id", "grund": "Ungültige Delegations-ID.", "steered": False}
    if isinstance(task, bool) or not isinstance(task, int) or not 0 <= task < 10000:
        return 400, {"error": "bad task_index", "grund": "Ungültiger Task-Index.", "steered": False}
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_STEER_TEXT:
        return 400, {"error": "bad text", "grund": f"Bitte einen Text senden (höchstens {MAX_STEER_TEXT} "
                                                  "Zeichen).", "steered": False}
    mine = {s for s in session_ids if s}
    found = [(sid, r) for sid, r in _records()
             if r.get("delegation_id") == deleg and _task_of(r) == task
             and (r.get("owner_agent_session_id") in mine or r.get("owner_session_id") in mine)]
    if len(found) != 1:
        return 404, {"error": "unknown subagent", "steered": False,
                     "grund": "Diesen Subagenten gibt es in dieser Sitzung nicht (mehr)."}
    sid, record = found[0]
    if not record.get("accepting_steer", False):
        return 409, {"error": "finished", "steered": False, "grund": "Subagent beendet — er nimmt nichts mehr an."}
    from tools import delegate_tool_registry as reg
    if not reg.steer_subagent(sid, text.strip()):
        return 409, {"error": "finished", "steered": False, "grund": "Subagent beendet — er nimmt nichts mehr an."}
    return 200, {"steered": True}

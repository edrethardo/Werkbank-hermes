"""WB-1338: the board writes to a running SUBAGENT of an admin chat.

``POST /v1/s/{key}/subagents/steer {delegation_id, task_index, text}`` — the plugin finds the live
child in the delegate registry by its delegation id and transcript (``task-<n>.log``), checks that
it belongs to THIS session (the chat the board resolved), and queues the text with
``steer_subagent`` (delivered at the child's next iteration boundary). A finished child: 409; a
child of another chat or an unknown one: 404 — never a guess.
"""

from __future__ import annotations

import pytest

from werkbank_testkit import SID, FakeHost, call, running_server, session_path

from tools import delegate_tool_registry as reg

DELEG = "deleg_ab12cd34"


class _Agent:
    def __init__(self, task, deleg=DELEG):
        self._live_transcript_path = f"/x/cache/delegation/live/{deleg}/task-{task}.log"
        self.steered: list = []

    def steer(self, text):
        self.steered.append(text)
        return True


@pytest.fixture
def srv(tmp_path):
    s = running_server(tmp_path, FakeHost())
    assert call(s.port, "GET", f"/v1/resolve?session_id={SID}", s.key)[0] == 200
    yield s
    s.server.stop()


@pytest.fixture
def children():
    added = []

    def add(sid, task, *, owner=SID, accepting=True, deleg=DELEG):
        agent = _Agent(task, deleg)
        reg._register_subagent({"subagent_id": sid, "delegation_id": deleg, "agent": agent,
                                "owner_agent_session_id": owner, "status": "running"})
        if not accepting:
            reg._active_subagents[sid]["accepting_steer"] = False
        added.append(sid)
        return agent
    yield add
    for sid in added:
        reg._active_subagents.pop(sid, None)


def steer(srv, **body):
    return call(srv.port, "POST", session_path("subagents", "steer"), srv.key, body)


def test_steer_reaches_the_live_child_of_this_session(srv, children):
    other = children("sa-0", 0)
    mine = children("sa-1", 1)
    status, body = steer(srv, delegation_id=DELEG, task_index=1, text="Bitte zuerst die Tests.")
    assert status == 200 and body == {"steered": True}
    assert mine.steered == ["Bitte zuerst die Tests."]
    assert other.steered == []


def test_finished_child_is_refused(srv, children):
    agent = children("sa-0", 0, accepting=False)
    status, body = steer(srv, delegation_id=DELEG, task_index=0, text="hallo")
    assert status == 409 and body["steered"] is False and "beendet" in body["grund"]
    assert agent.steered == []


def test_child_of_another_chat_or_unknown_is_404(srv, children):
    agent = children("sa-0", 0, owner="20990101_000000_ffffff")
    assert steer(srv, delegation_id=DELEG, task_index=0, text="x")[0] == 404
    assert steer(srv, delegation_id="deleg_00000000", task_index=0, text="x")[0] == 404
    assert agent.steered == []


@pytest.mark.parametrize("body", [
    {"delegation_id": "../x", "task_index": 0, "text": "x"},
    {"delegation_id": DELEG, "task_index": "0", "text": "x"},
    {"delegation_id": DELEG, "task_index": -1, "text": "x"},
    {"delegation_id": DELEG, "task_index": 0, "text": "   "},
    {"delegation_id": DELEG, "task_index": 0, "text": "x" * 20001},
])
def test_bad_requests(srv, children, body):
    agent = children("sa-0", 0)
    assert steer(srv, **body)[0] == 400
    assert agent.steered == []

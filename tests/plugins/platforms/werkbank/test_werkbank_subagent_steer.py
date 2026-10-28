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


def test_ownership_check_and_steer_hold_one_registry_lock(srv, children, monkeypatch):
    """Review WB-1338: between the ownership check and the steer the record must not change. A
    recycled id (the record swapped for another chat's child right after a lock release) never
    gets the text: check and ``agent.steer`` run under ONE ``_active_subagents_lock``."""
    mine = children("sa-1", 1)
    foreign = _Agent(1)
    real = reg._active_subagents_lock

    class _SwapOnRelease:
        swapped = False

        def __enter__(self):
            real.acquire()
            return self

        def __exit__(self, *exc):
            real.release()
            if not _SwapOnRelease.swapped:
                _SwapOnRelease.swapped = True
                reg._active_subagents["sa-1"] = {
                    "subagent_id": "sa-1", "delegation_id": DELEG, "agent": foreign,
                    "owner_agent_session_id": "20990101_000000_ffffff", "status": "running",
                    "accepting_steer": True}
            return False

        def acquire(self, *a, **kw):
            return real.acquire(*a, **kw)

        def release(self):
            return real.release()

        def locked(self):
            return real.locked()

    monkeypatch.setattr(reg, "_active_subagents_lock", _SwapOnRelease())
    status, body = steer(srv, delegation_id=DELEG, task_index=1, text="nur für meins")
    assert foreign.steered == [], "die Zeile ging an das Kind einer anderen Sitzung"
    assert (status, mine.steered) == (200, ["nur für meins"])


def test_steer_text_never_reaches_the_gateway_log(srv, children, caplog):
    import logging as _logging
    children("sa-1", 1)
    secret_line = "streng-vertraulich-7f3a"
    with caplog.at_level(_logging.DEBUG):
        assert steer(srv, delegation_id=DELEG, task_index=1, text=secret_line)[0] == 200
        assert steer(srv, delegation_id=DELEG, task_index=9, text=secret_line)[0] == 404
    assert secret_line not in caplog.text

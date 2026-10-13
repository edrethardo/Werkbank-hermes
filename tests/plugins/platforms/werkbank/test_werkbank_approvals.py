"""WB-1129: answering approvals and clarify prompts from the Werkbank (exact request id only).

The pending approvals are REAL entries of ``tools.approval`` (``_ApprovalEntry`` in
``_gateway_queues``) and real ``tools.clarify_gateway`` entries: the plugin must resolve
exactly the one the board named, nothing else, and nothing at all for an id that is not
pending in THIS session.
"""

from __future__ import annotations

import threading
import time

import pytest

from werkbank_testkit import KEY_SESSION, SID, call, session_path

from tools import approval as A
from tools import approval_context
from tools import clarify_gateway
from tools.approval_gateway_wait import _ApprovalEntry

OTHER_KEY = "agent:main:telegram:dm:999:7"


@pytest.fixture(autouse=True)
def _clean_queues():
    def scrub():
        with A._lock:
            for key in (KEY_SESSION, OTHER_KEY):
                A._gateway_queues.pop(key, None)
                A._gateway_notify_cbs.pop(key, None)
                A._session_approved.pop(key, None)
            getattr(A, "_recent_decisions", {}).clear()
        with clarify_gateway._lock:
            for key in (KEY_SESSION, OTHER_KEY):
                for cid in clarify_gateway._session_index.pop(key, []):
                    clarify_gateway._entries.pop(cid, None)
    scrub()
    yield
    scrub()


def _resolve(srv):
    status, answer = call(srv.port, "GET", f"/v1/resolve?session_id={SID}", srv.key)
    assert status == 200, answer


def pend(key, rid, **data):
    entry = _ApprovalEntry({"request_id": rid, "command": f"rm -rf /tmp/{rid}", "pattern_key": "rm",
                            "pattern_keys": ["rm"], **data})
    with A._lock:
        A._gateway_queues.setdefault(key, []).append(entry)
    return entry


def post_approval(srv, rid, choice):
    return call(srv.port, "POST", session_path("approvals", rid), srv.key, {"choice": choice})


# ── Fertig, wenn 1: two open approvals, answered in reverse order ──────────────────────────────


def test_two_open_approvals_answered_in_reverse_order_each_hit_the_right_one(plugin_server):
    srv = plugin_server
    _resolve(srv)
    first, second = pend(KEY_SESSION, "req-A"), pend(KEY_SESSION, "req-B")

    assert post_approval(srv, "req-B", "deny") == (200, {"resolved": 1, "choice": "deny",
                                                         "decided_by": "werkbank"})
    assert second.event.is_set() and second.result == "deny" and second.decided_by == "werkbank"
    assert not first.event.is_set() and first.result is None
    assert [d["request_id"] for d in A.list_gateway_approvals(KEY_SESSION)] == ["req-A"]

    assert post_approval(srv, "req-A", "once") == (200, {"resolved": 1, "choice": "once",
                                                         "decided_by": "werkbank"})
    assert first.event.is_set() and first.result == "once" and first.decided_by == "werkbank"
    assert A.list_gateway_approvals(KEY_SESSION) == []


# ── Fertig, wenn 3: a foreign or invented request id → 404, nothing resolved ────────────────────


@pytest.mark.parametrize("rid", ["req-foreign", "erfunden", "req-A2"])
def test_foreign_or_invented_request_id_is_404_and_resolves_nothing(plugin_server, rid):
    srv = plugin_server
    _resolve(srv)
    own = pend(KEY_SESSION, "req-A")
    foreign = pend(OTHER_KEY, "req-foreign")              # pending, but in ANOTHER session

    status, answer = post_approval(srv, rid, "once")
    assert status == 404 and isinstance(answer.get("grund"), str)
    assert not own.event.is_set() and own.result is None
    assert not foreign.event.is_set() and foreign.result is None
    assert [d["request_id"] for d in A.list_gateway_approvals(KEY_SESSION)] == ["req-A"]
    assert [d["request_id"] for d in A.list_gateway_approvals(OTHER_KEY)] == ["req-foreign"]


def test_unresolved_session_key_is_404_even_with_a_pending_request(plugin_server):
    srv = plugin_server
    _resolve(srv)
    foreign = pend(OTHER_KEY, "req-X")
    status, _ = call(srv.port, "POST", session_path("approvals", "req-X", key=OTHER_KEY), srv.key,
                     {"choice": "once"})
    assert status == 404
    assert not foreign.event.is_set()


@pytest.mark.parametrize("body", [{}, {"choice": "yes"}, {"choice": "ONCE"}, {"choice": 1}])
def test_bad_choice_is_400_and_resolves_nothing(plugin_server, body):
    srv = plugin_server
    _resolve(srv)
    own = pend(KEY_SESSION, "req-A")
    status, _ = call(srv.port, "POST", session_path("approvals", "req-A"), srv.key, body)
    assert status == 400
    assert not own.event.is_set()


def test_bad_request_id_is_400(plugin_server):
    srv = plugin_server
    _resolve(srv)
    status, _ = post_approval(srv, "../x", "once")
    assert status in (400, 404)


# ── the same flags as the Telegram card (_approval_notify_sync) ─────────────────────────────────


@pytest.mark.parametrize("flags,refused,allowed", [
    ({"allow_session": False}, "session", "always"),
    ({"allow_permanent": False}, "always", "session"),
    ({"smart_denied": True, "allow_permanent": False, "allow_session": False}, "always", "once"),
    ({"smart_denied": True}, "session", "deny"),
])
def test_scope_the_card_does_not_offer_is_400(plugin_server, flags, refused, allowed):
    srv = plugin_server
    _resolve(srv)
    entry = pend(KEY_SESSION, "req-A", **flags)
    status, answer = post_approval(srv, "req-A", refused)
    assert status == 400 and isinstance(answer.get("grund"), str)
    assert not entry.event.is_set()
    assert post_approval(srv, "req-A", allowed)[1]["resolved"] == 1
    assert entry.result == allowed


# ── child (subagent) requests: once/deny only; a parent's answer never frees a child ──────────


@pytest.mark.parametrize("scope", ["session", "always"])
def test_child_request_refuses_session_and_always(plugin_server, scope):
    srv = plugin_server
    _resolve(srv)
    child = pend(KEY_SESSION, "req-C", delegated_child=True, child_session_id="child-sid")
    status, answer = post_approval(srv, "req-C", scope)
    assert status == 400 and "Subagent" in answer["grund"]
    assert not child.event.is_set() and child.result is None
    assert post_approval(srv, "req-C", "once")[1]["resolved"] == 1
    assert child.result == "once" and child.decided_by == "werkbank"


@pytest.mark.parametrize("choice", ["once", "session", "always", "deny"])
def test_parent_answer_never_releases_a_child_request(plugin_server, choice):
    """Sicherheitsreview: the parent's approval (any scope) resolves the parent's request only."""
    srv = plugin_server
    _resolve(srv)
    parent = pend(KEY_SESSION, "req-P")
    child = pend(KEY_SESSION, "req-C", delegated_child=True, child_session_id="child-sid")
    assert post_approval(srv, "req-P", choice)[1]["resolved"] == 1
    assert parent.result == choice
    assert not child.event.is_set() and child.result is None
    assert [d["request_id"] for d in A.list_gateway_approvals(KEY_SESSION)] == ["req-C"]
    # and the child's id answered "once" frees the child only
    assert post_approval(srv, "req-C", "once")[1]["resolved"] == 1
    assert child.result == "once"


# ── the real waiter: stamps the child, emits approval.resolved with decided_by ─────────────────


@pytest.fixture
def recorded_events(monkeypatch):
    from gateway import session_events
    seen = []
    monkeypatch.setattr(session_events, "enabled", lambda: True)
    monkeypatch.setattr(session_events, "emit",
                        lambda key, sid, origin, type_, id_, parent, payload, **_k:
                        seen.append({"session_key": key, "type": type_, "id": id_, "payload": payload}))
    monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 5)
    return seen


def _wait_in_thread(key, data, *, child_sid=None):
    from tools.approval_gateway_wait import _await_gateway_decision
    out = {}

    def run():
        if child_sid is None:
            out["decision"] = _await_gateway_decision(key, lambda _d: None, data)
            return
        from agent.delegation_context import delegated_child_context
        try:
            with delegated_child_context(child_sid):
                out["decision"] = _await_gateway_decision(key, lambda _d: None, data)
        except BaseException:
            import traceback
            out["error"] = traceback.format_exc()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not A.list_gateway_approvals(key) and time.monotonic() < deadline:
        time.sleep(0.01)
    return thread, out


def test_real_waiter_child_is_stamped_and_resolved_event_names_werkbank(plugin_server, recorded_events):
    srv = plugin_server
    _resolve(srv)
    thread, out = _wait_in_thread(KEY_SESSION, {"command": "rm -rf /tmp/kind", "pattern_key": "rm",
                                                "pattern_keys": ["rm"]}, child_sid="child-sid-1")
    pending = A.list_gateway_approvals(KEY_SESSION)
    assert len(pending) == 1, (out, recorded_events)
    assert pending[0]["delegated_child"] is True and pending[0]["child_session_id"] == "child-sid-1"
    deadline = time.monotonic() + 5                 # the request event follows the enqueue
    while not any(e["type"] == "approval.request" for e in recorded_events) and time.monotonic() < deadline:
        time.sleep(0.01)
    request = [e for e in recorded_events if e["type"] == "approval.request"][0]
    assert request["payload"]["delegated_child"] is True
    assert request["payload"]["child_session_id"] == "child-sid-1"
    rid = pending[0]["request_id"]

    assert post_approval(srv, rid, "always")[0] == 400           # a child: once/deny only
    assert post_approval(srv, rid, "once")[1]["resolved"] == 1
    thread.join(5)
    assert out["decision"]["choice"] == "once" and out["decision"]["resolved"] is True
    resolved = [e for e in recorded_events if e["type"] == "approval.resolved"]
    assert resolved and resolved[-1]["payload"] == {"request_id": rid, "choice": "once", "decided_by": "werkbank"}


def test_real_waiter_parent_is_not_marked_as_child(plugin_server, recorded_events):
    thread, _ = _wait_in_thread(KEY_SESSION, {"command": "rm -rf /tmp/eltern", "pattern_key": "rm",
                                              "pattern_keys": ["rm"]})
    pending = A.list_gateway_approvals(KEY_SESSION)
    assert not pending[0].get("delegated_child") and "child_session_id" not in pending[0]
    A.resolve_gateway_approval(KEY_SESSION, "deny", request_id=pending[0]["request_id"])
    thread.join(5)


# ── Fertig, wenn 2 (plugin side): the loser learns the winner ──────────────────────────────────


def test_answer_after_telegram_won_reports_the_winner_with_resolved_0(plugin_server):
    srv = plugin_server
    _resolve(srv)
    entry = pend(KEY_SESSION, "req-A")
    # the plugin saw req-A pending, then Telegram answered first
    real_list = A.list_gateway_approvals

    def list_then_telegram_wins(key):
        listed = real_list(key)
        A.resolve_gateway_approval(KEY_SESSION, "deny", request_id="req-A", decided_by="telegram")
        return listed

    import tools.approval
    orig = tools.approval.list_gateway_approvals
    tools.approval.list_gateway_approvals = list_then_telegram_wins
    try:
        status, answer = post_approval(srv, "req-A", "once")
    finally:
        tools.approval.list_gateway_approvals = orig
    assert (status, answer) == (200, {"resolved": 0, "choice": "deny", "decided_by": "telegram"})
    assert entry.result == "deny" and entry.decided_by == "telegram"


def test_concurrent_answers_exactly_one_wins(plugin_server):
    srv = plugin_server
    _resolve(srv)
    for round_ in range(20):
        entry = pend(KEY_SESSION, f"req-{round_}")
        barrier = threading.Barrier(2)
        results = {}

        def werkbank():
            barrier.wait()
            results["werkbank"] = post_approval(srv, f"req-{round_}", "once")[1]

        def telegram():
            barrier.wait()
            results["telegram"] = A.resolve_gateway_approval(KEY_SESSION, "deny", request_id=f"req-{round_}",
                                                             decided_by="telegram")

        threads = [threading.Thread(target=werkbank), threading.Thread(target=telegram)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        assert sorted([results["werkbank"]["resolved"], results["telegram"]]) == [0, 1]
        # both report the one outcome the agent got
        assert results["werkbank"]["choice"] == entry.result
        assert results["werkbank"]["decided_by"] == entry.decided_by


# ── clarify ─────────────────────────────────────────────────────────────────────────────────


def post_clarify(srv, cid, answer, key=KEY_SESSION):
    return call(srv.port, "POST", session_path("clarify", cid, key=key), srv.key, {"answer": answer})


def test_clarify_answer_resolves_exactly_that_prompt(plugin_server):
    srv = plugin_server
    _resolve(srv)
    one = clarify_gateway.register("clar-1", KEY_SESSION, "Welche?", ["A", "B"])
    two = clarify_gateway.register("clar-2", KEY_SESSION, "Wohin?", None)
    assert post_clarify(srv, "clar-2", "nach Hause") == (200, {"resolved": True})
    assert two.event.is_set() and two.response == "nach Hause" and two.decided_by == "werkbank"
    assert not one.event.is_set()
    assert post_clarify(srv, "clar-1", "B") == (200, {"resolved": True})
    assert one.response == "B" and one.decided_by == "werkbank"
    # answered already: no second answer
    assert post_clarify(srv, "clar-1", "A") == (200, {"resolved": False})
    assert one.response == "B"


def test_clarify_of_another_session_or_invented_is_404(plugin_server):
    srv = plugin_server
    _resolve(srv)
    foreign = clarify_gateway.register("clar-f", OTHER_KEY, "Fremd?", ["ja", "nein"])
    for cid in ("clar-f", "clar-erfunden"):
        status, _ = post_clarify(srv, cid, "ja")
        assert status == 404
    assert not foreign.event.is_set() and foreign.response is None


def test_clarify_answer_outside_the_choices_is_400(plugin_server):
    srv = plugin_server
    _resolve(srv)
    entry = clarify_gateway.register("clar-1", KEY_SESSION, "Welche?", ["A", "B"])
    status, answer = post_clarify(srv, "clar-1", "irgendwas ganz anderes")
    assert status == 400 and isinstance(answer.get("grund"), str)
    assert not entry.event.is_set()
    for bad in ({}, {"answer": 3}):
        assert call(srv.port, "POST", session_path("clarify", "clar-1"), srv.key, bad)[0] == 400
    assert post_clarify(srv, "clar-1", "2") == (200, {"resolved": True})       # number = Telegram's pick
    assert entry.response == "B"


def test_clarify_resolved_event_names_werkbank(plugin_server, recorded_events):
    srv = plugin_server
    _resolve(srv)
    entry = clarify_gateway.register("clar-1", KEY_SESSION, "Welche?", ["A", "B"])
    out = {}
    thread = threading.Thread(target=lambda: out.update(r=clarify_gateway.wait_for_response("clar-1", 5)))
    thread.start()
    assert post_clarify(srv, "clar-1", "A") == (200, {"resolved": True})
    thread.join(5)
    assert out["r"] == "A"
    resolved = [e for e in recorded_events if e["type"] == "clarify.resolved"]
    assert resolved[-1]["payload"]["decided_by"] == "werkbank"
    assert resolved[-1]["payload"]["answer"] == "A"
    assert entry.decided_by == "werkbank"


# ── subagent name for the board's card ────────────────────────────────────────────────────────


def test_child_request_carries_the_subagent_id_in_stream_and_snapshot(plugin_server, monkeypatch):
    from werkbank_testkit import core_event
    srv = plugin_server
    _resolve(srv)
    srv.buffers.on_event(core_event(1, "subagent.start", {"goal": "Tests laufen lassen",
                                                          "child_session_id": "child-sid-9"},
                                    id_="sa-0-abc", parent_id="turn-1"))
    srv.buffers.on_event(core_event(2, "approval.request", {"request_id": "req-C", "delegated_child": True,
                                                            "child_session_id": "child-sid-9"},
                                    id_="req-C"))
    event = srv.buffers.get(KEY_SESSION).events[-1]
    assert event["payload"]["subagent_id"] == "sa-0-abc"

    srv.host.approvals = [{"request_id": "req-C", "delegated_child": True, "child_session_id": "child-sid-9"}]
    status, snap = call(srv.port, "GET", session_path("snapshot"), srv.key)
    assert status == 200
    assert snap["pending_approvals"][0]["subagent_id"] == "sa-0-abc"

"""WB-1128 review 4: a buffered entry only bridges the time until its SessionDB row exists.

Once the row was seen (or the core reported it written), the entry leaves the buffer for good: a
later compression (in place or by rotation) or ``/undo`` that takes the row out of the active
history must never bring the buffered copy back. Independent of a full window, an entry whose row
(or mark) lies below the first row of the active history is not shown (row ids only grow).
"""

from __future__ import annotations

import pytest

from werkbank_testkit import KEY_SESSION, SID, FakeHost, core_event

CHILD = "20261013_130000_aaaaaa"


@pytest.fixture
def env():
    from plugins.platforms.werkbank import buffer

    host = FakeHost()
    buffers = buffer.Buffers(plugin_id="platforms/werkbank", watermark=host.row_watermark)
    buffers.watch(KEY_SESSION, SID, "telegram")
    return host, buffers


def _ev(seq, type_, payload=None, *, id_=None, sid=SID, origin="telegram"):
    return core_event(seq, type_, payload, id_=id_, sid=sid, origin=origin)


def _snap(host, buffers):
    from plugins.platforms.werkbank import snapshot

    return snapshot.build(buffers, host, KEY_SESSION)


def _shown(snap):
    return [(m["id"], m["text"], bool(m.get("from_buffer"))) for m in snap["messages"]]


def _buffered(snap):
    return [m["id"] for m in snap["messages"] if m.get("from_buffer")]


def _pos(buffers):
    return buffers.position(KEY_SESSION)


# ── the three repros of review 3 ─────────────────────────────────────────────────────────────


def test_c1_in_place_compaction_does_not_revive_a_named_answer(env):
    host, buffers = env
    buffers.on_event(_ev(1, "turn.start", {}, id_="t1"))
    host.messages = [{"id": 1, "role": "user", "content": "frage"},
                     {"id": 2, "role": "assistant", "content": "Antwort A", "message_uid": "u-A"}]
    buffers.on_event(_ev(2, "turn.done", {"final_response": "Antwort A", "message_uid": "u-A", "row_id": 2},
                         id_="t1"))
    assert _shown(_snap(host, buffers)) == [("db:1", "frage", False), ("db:2", "Antwort A", False)]
    host.messages = [{"id": 3, "role": "user", "content": "[Zusammenfassung]", "message_uid": "u-S"}]
    assert _buffered(_snap(host, buffers)) == []


def test_c2_rotation_does_not_revive_an_answer_found_by_row_marks(env):
    host, buffers = env
    buffers.on_event(_ev(1, "turn.start", {}, id_="t1"))
    host.messages = [{"id": 1, "role": "user", "content": "frage"}, {"id": 2, "role": "assistant", "content": "B"}]
    buffers.on_event(_ev(2, "turn.done", {"final_response": "B"}, id_="t1"))
    host.sessions = {CHILD: {"session_key": KEY_SESSION, "session_id": CHILD, "platform": "telegram", "live": True}}
    host.lineages = {CHILD: [SID, CHILD]}
    host.messages = [{"id": 5, "role": "user", "content": "[Zusammenfassung]"}]
    assert _buffered(_snap(host, buffers)) == []


def test_c3_undo_does_not_revive_a_named_answer(env):
    host, buffers = env
    buffers.on_event(_ev(1, "turn.start", {}, id_="t1"))
    host.messages = [{"id": 1, "role": "user", "content": "frage"},
                     {"id": 2, "role": "assistant", "content": "C", "message_uid": "u-C"}]
    buffers.on_event(_ev(2, "turn.done", {"final_response": "C", "message_uid": "u-C", "row_id": 2}, id_="t1"))
    host.messages = []
    assert _buffered(_snap(host, buffers)) == []


# ── (a) confirmed once = gone for good ──────────────────────────────────────────────────────


def test_a_answer_seen_in_a_snapshot_leaves_the_buffer(env):
    host, buffers = env
    buffers.on_event(_ev(1, "turn.start", {}, id_="t1"))
    buffers.on_event(_ev(2, "turn.done", {"final_response": "A", "message_uid": "u-A", "row_id": 2}, id_="t1"))
    assert _buffered(_snap(host, buffers)) == ["t1"]                    # row not written yet
    host.messages = [{"id": 2, "role": "assistant", "content": "A", "message_uid": "u-A"}]
    assert _buffered(_snap(host, buffers)) == []
    assert _pos(buffers)["recent_answers"] == []                        # confirmed: removed
    host.messages = []                                                  # /undo afterwards
    assert _buffered(_snap(host, buffers)) == []


def test_a_answer_written_before_turn_done_is_confirmed_on_arrival(env):
    host, buffers = env
    host.messages = [{"id": 4, "role": "assistant", "content": "A", "message_uid": "u-A"}]
    buffers.on_event(_ev(1, "turn.done", {"final_response": "A", "message_uid": "u-A", "row_id": 4}, id_="t1"))
    assert _pos(buffers)["recent_answers"] == []


def test_a_fallback_answer_found_once_leaves_the_buffer(env):
    host, buffers = env
    buffers.on_event(_ev(1, "turn.start", {}, id_="t1"))
    host.messages = [{"id": 1, "role": "user", "content": "frage"}, {"id": 2, "role": "assistant", "content": "B"}]
    buffers.on_event(_ev(2, "turn.done", {"final_response": "B"}, id_="t1"))
    assert _buffered(_snap(host, buffers)) == []
    assert _pos(buffers)["recent_answers"] == []
    host.messages = [{"id": 1, "role": "user", "content": "frage"}]     # the answer row rewound
    assert _buffered(_snap(host, buffers)) == []


def test_a_user_message_seen_once_leaves_the_buffer(env):
    host, buffers = env
    buffers.on_event(_ev(1, "user.message", {"text": "hallo", "message_id": "tg-9"}, id_="tg-9"))
    assert _buffered(_snap(host, buffers)) == ["tg-9"]
    host.messages = [{"id": 1, "role": "user", "content": "hallo", "platform_message_id": "tg-9"}]
    assert _shown(_snap(host, buffers)) == [("tg-9", "hallo", False)]
    assert _pos(buffers)["recent_users"] == []
    host.messages = [{"id": 7, "role": "user", "content": "[Zusammenfassung]"}]   # compaction lost the id
    assert _buffered(_snap(host, buffers)) == []


# ── (b) below the first row of the active history: not shown, window full or not ─────────────


def test_b_named_answer_below_the_first_history_row_is_not_shown(env):
    host, buffers = env
    buffers.on_event(_ev(1, "turn.done", {"final_response": "A", "message_uid": "u-A", "row_id": 2}, id_="t1"))
    host.messages = [{"id": 3, "role": "user", "content": "[Zusammenfassung]"}]      # never seen row 2
    assert _buffered(_snap(host, buffers)) == []


def test_b_fallback_answer_whose_mark_lies_below_the_history_is_not_shown(env):
    host, buffers = env
    host.messages = [{"id": 1, "role": "user", "content": "frage"}, {"id": 2, "role": "assistant", "content": "B"}]
    buffers.on_event(_ev(1, "turn.start", {}, id_="t1"))
    buffers.on_event(_ev(2, "turn.done", {"final_response": "B"}, id_="t1"))   # rows 1..2 written, mark 2
    host.messages = [{"id": 3, "role": "user", "content": "[Zusammenfassung]"}]     # in-place compaction
    assert _buffered(_snap(host, buffers)) == []


def test_b_user_message_whose_mark_lies_below_the_history_is_not_shown(env):
    host, buffers = env
    host.messages = [{"id": 1, "role": "user", "content": "alt"}]
    buffers.on_event(_ev(1, "user.message", {"text": "x", "message_id": "tg-1"}, id_="tg-1"))
    host.messages = [{"id": 2, "role": "user", "content": "x"}, {"id": 3, "role": "user", "content": "[Z]"}]
    host.messages = host.messages[1:]                                    # its row compacted away
    assert _buffered(_snap(host, buffers)) == []


def test_b_an_unwritten_answer_of_the_running_session_still_shows(env):
    host, buffers = env
    host.messages = [{"id": 1, "role": "user", "content": "frage"}]
    buffers.on_event(_ev(1, "turn.start", {}, id_="t1"))
    buffers.on_event(_ev(2, "turn.done", {"final_response": "A", "message_uid": "u-A", "row_id": 2}, id_="t1"))
    assert _shown(_snap(host, buffers)) == [("db:1", "frage", False), ("t1", "A", True)]


# ── (c) a new session id (rotation, /new): the old id's answers are dropped ───────────────────


def test_c_session_change_drops_answers_of_the_old_session_id(env):
    host, buffers = env
    buffers.on_event(_ev(1, "turn.start", {}, id_="t1"))
    buffers.on_event(_ev(2, "turn.done", {"final_response": "alt"}, id_="t1"))
    buffers.on_event(_ev(3, "turn.start", {}, id_="t2", sid=CHILD))
    assert [a["turn_id"] for a in _pos(buffers)["recent_answers"]] == []
    buffers.on_event(_ev(4, "turn.done", {"final_response": "neu"}, id_="t2", sid=CHILD))
    assert [a["turn_id"] for a in _pos(buffers)["recent_answers"]] == ["t2"]


# ── (d) only the turns since the last confirmed answer ───────────────────────────────────────


def test_d_answers_older_than_a_confirmed_answer_are_dropped(env):
    host, buffers = env
    buffers.on_event(_ev(1, "turn.start", {}, id_="t_a"))
    buffers.on_event(_ev(2, "turn.done", {"final_response": "A"}, id_="t_a"))       # row never written
    buffers.on_event(_ev(3, "turn.start", {}, id_="t_b"))
    host.messages = [{"id": 1, "role": "assistant", "content": "B", "message_uid": "u-B"}]
    buffers.on_event(_ev(4, "turn.done", {"final_response": "B", "message_uid": "u-B", "row_id": 1}, id_="t_b"))
    assert _buffered(_snap(host, buffers)) == []
    assert _pos(buffers)["recent_answers"] == []


def test_d_a_burst_of_old_unconfirmed_answers_never_reaches_the_snapshot(env):
    host, buffers = env
    seq = 0
    for n in range(25):
        seq += 1
        buffers.on_event(_ev(seq, "turn.start", {}, id_=f"t{n}"))
        host.messages.append({"id": n + 1, "role": "assistant", "content": f"A{n}", "message_uid": f"u{n}"})
        seq += 1
        buffers.on_event(_ev(seq, "turn.done", {"final_response": f"A{n}", "message_uid": f"u{n}",
                                                "row_id": n + 1}, id_=f"t{n}"))
    host.messages = [{"id": 99, "role": "user", "content": "[Zusammenfassung]"}]
    assert _buffered(_snap(host, buffers)) == []


# ── an empty / invisible answer row never stands for the buffered answer ─────────────────────


def test_empty_assistant_row_with_the_uid_does_not_hide_the_buffered_answer(env):
    host, buffers = env
    host.messages = [{"id": 1, "role": "user", "content": "frage"}]
    buffers.on_event(_ev(1, "turn.start", {}, id_="t1"))
    buffers.on_event(_ev(2, "turn.done", {"final_response": "Fehlertext", "message_uid": "u-E", "row_id": 2},
                         id_="t1"))
    host.messages.append({"id": 2, "role": "assistant", "content": "", "message_uid": "u-E"})
    assert _buffered(_snap(host, buffers)) == ["t1"]


# ── rows of an older core: client_message_id without hermes_plugin_id ────────────────────────


def test_board_id_without_plugin_id_is_still_werkbank(env):
    host, buffers = env
    host.messages = [
        {"id": 1, "role": "user", "content": "a", "display_metadata": {"client_message_id": "wb-1f2e"}},
        {"id": 2, "role": "user", "content": "b", "display_metadata": {"client_message_id": "x-1"}},
    ]
    snap = _snap(host, buffers)
    assert [(m["id"], m["origin"]) for m in snap["messages"]] == [("wb-1f2e", "werkbank"), ("x-1", "telegram")]


# ── the watermark is MAX(id) through a public SessionDB method ───────────────────────────────


def test_host_watermark_reads_max_id_including_rewound_rows(tmp_path):
    from hermes_state import SessionDB
    from plugins.platforms.werkbank.host import GatewayHost

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id=SID, source="telegram")
        db.append_message(SID, "user", "a")
        last = db.append_message(SID, "assistant", "b")
        db.deactivate_message(SID, last)
        calls = []
        real = db.get_messages
        db.get_messages = lambda *a, **k: calls.append(a) or real(*a, **k)
        owner = type("Owner", (), {"session_store": None, "_session_db": db})()
        ctx = type("Ctx", (), {"_manager": type("M", (), {"_gateway_message_injector": (owner, None)})()})()
        host = GatewayHost(ctx, tmp_path)
        assert host.row_watermark(KEY_SESSION, SID) == last
        assert calls == []                                    # no message bodies read for a mark
        assert db.max_message_row_id(SID) == last
        assert db.max_message_row_id("unknown") == 0
    finally:
        db.close()

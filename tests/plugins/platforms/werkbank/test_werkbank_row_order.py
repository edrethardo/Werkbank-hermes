"""WB-1128 review 3: which SessionDB row stands for a buffered answer is decided by row ids (they
only grow: ``messages.id`` is AUTOINCREMENT) or by the answer's own ``message_uid`` — never by a
clock. Buffered entries belong to one session id: after ``/new`` the old ones are gone, through a
compression chain they stay."""

from __future__ import annotations

import pytest

from werkbank_testkit import KEY_SESSION, SID, FakeHost, core_event


@pytest.fixture
def env():
    from plugins.platforms.werkbank import buffer

    host = FakeHost()
    buffers = buffer.Buffers(plugin_id="platforms/werkbank", watermark=host.row_watermark)
    return host, buffers


def _ev(seq, type_, payload=None, *, id_=None, sid=SID, ts="2026-10-13T12:00:00.000000Z", origin="telegram"):
    event = core_event(seq, type_, payload, id_=id_, sid=sid, origin=origin)
    event["ts"] = ts
    return event


def _snap(host, buffers):
    from plugins.platforms.werkbank import snapshot

    return snapshot.build(buffers, host, KEY_SESSION)


def _shown(snap, role=None):
    return [(m["id"], m["text"]) for m in snap["messages"] if role is None or m["role"] == role]


def _answer(row_id, text, **extra):
    return {"id": row_id, "role": "assistant", "content": text, "timestamp": 1.0, **extra}


# ── R1: the board opens the session mid-turn (its turn.start was never seen) ────────────────────


def test_r1_answer_of_a_turn_begun_before_observation_is_not_doubled(env):
    host, buffers = env
    host.messages = [{"id": 1, "role": "user", "content": "frage", "timestamp": 1.0}]
    buffers.watch(KEY_SESSION, SID, "telegram")                         # observation mark: row 1
    buffers.on_event(_ev(10, "turn.done", {"final_response": "Antwort A"}, id_="t1"))
    assert _shown(_snap(host, buffers), "assistant") == [("t1", "Antwort A")]     # row not yet there
    host.messages.append(_answer(2, "Antwort A"))
    assert _shown(_snap(host, buffers), "assistant") == [("db:2", "Antwort A")]


# ── R2b: the wall clock steps back between two turns ──────────────────────────────────────────


@pytest.mark.parametrize("ts_b", ["2026-10-13T12:00:02.000000Z", "2026-10-13T11:59:58.000000Z",
                                  "2026-10-13T12:00:00.000000Z"])
def test_r2b_turns_are_told_apart_by_row_ids_not_by_the_clock(env, ts_b):
    host, buffers = env
    buffers.watch(KEY_SESSION, SID, "telegram")
    buffers.on_event(_ev(1, "turn.start", {}, id_="t1", ts="2026-10-13T12:00:00.000000Z"))
    host.messages.append({**_answer(2, "A"), "timestamp": 1791892804.0})
    buffers.on_event(_ev(2, "turn.done", {"final_response": "A"}, id_="t1", ts="2026-10-13T12:00:05.000000Z"))
    buffers.on_event(_ev(3, "turn.start", {}, id_="t2", ts=ts_b))
    buffers.on_event(_ev(4, "turn.done", {"final_response": "B"}, id_="t2", ts=ts_b))
    assert _shown(_snap(host, buffers), "assistant") == [("db:2", "A"), ("t2", "B")]
    host.messages.append({**_answer(3, "B"), "timestamp": 1791892700.0})       # an older wall time
    assert _shown(_snap(host, buffers), "assistant") == [("db:2", "A"), ("db:3", "B")]


def test_same_text_of_an_earlier_turn_does_not_stand_for_the_new_answer(env):
    host, buffers = env
    host.messages = [_answer(1, "Erledigt.")]
    buffers.watch(KEY_SESSION, SID, "telegram")
    buffers.on_event(_ev(1, "turn.start", {}, id_="t_b"))
    buffers.on_event(_ev(2, "turn.done", {"final_response": "Erledigt."}, id_="t_b"))
    assert [m["id"] for m in _snap(host, buffers)["messages"]] == ["db:1", "t_b"]
    host.messages.append(_answer(2, "Erledigt."))
    assert [m["id"] for m in _snap(host, buffers)["messages"]] == ["db:1", "db:2"]


def test_an_answer_row_counts_for_its_own_turn_only(env):
    host, buffers = env
    buffers.watch(KEY_SESSION, SID, "telegram")
    buffers.on_event(_ev(1, "turn.start", {}, id_="t_a"))
    host.messages.append({"id": 1, "role": "assistant", "content": "", "timestamp": 1.0,
                          "tool_calls": [{"id": "c1", "function": {"name": "terminal"}}]})
    buffers.on_event(_ev(2, "turn.done", {"final_response": "A fertig."}, id_="t_a"))   # row not written
    buffers.on_event(_ev(3, "turn.start", {}, id_="t_b"))
    host.messages.append(_answer(2, "B fertig."))                  # t_b's row: not t_a's answer
    assert [m["id"] for m in _snap(host, buffers)["messages"]] == ["db:1", "db:2", "t_a"]
    buffers.on_event(_ev(4, "turn.done", {"final_response": "B fertig."}, id_="t_b"))
    # review 4: once a later answer is confirmed, the older never-written one is dropped (no revival)
    assert [m["id"] for m in _snap(host, buffers)["messages"]] == ["db:1", "db:2"]


# ── the core names the answer's row: then only that id counts ─────────────────────────────────


def test_answer_named_by_message_uid_is_matched_by_that_id_only(env):
    host, buffers = env
    buffers.watch(KEY_SESSION, SID, "telegram")
    buffers.on_event(_ev(1, "turn.start", {}, id_="t1"))
    buffers.on_event(_ev(2, "turn.done", {"final_response": "B", "message_uid": "uid-b", "row_id": 7},
                         id_="t1"))
    host.messages = [_answer(5, "irgendwas", message_uid="uid-a")]     # after the mark, not this answer
    assert [m["id"] for m in _snap(host, buffers)["messages"]] == ["db:5", "t1"]
    host.messages.append(_answer(7, "B", message_uid="uid-b"))
    assert [m["id"] for m in _snap(host, buffers)["messages"]] == ["db:5", "db:7"]


def test_answer_uid_survives_a_compression_copy(env):
    host, buffers = env
    buffers.watch(KEY_SESSION, SID, "telegram")
    buffers.on_event(_ev(1, "turn.done", {"final_response": "B", "message_uid": "uid-b", "row_id": 3}, id_="t1"))
    host.messages = [_answer(40, "B", message_uid="uid-x", absorbed_message_uids=["uid-b"])]
    assert [m["id"] for m in _snap(host, buffers)["messages"]] == ["db:40"]


# ── R3: /new — the old session's buffered entries are not the new session's ──────────────────


def test_r3_after_new_the_old_sessions_buffered_entries_are_gone(env):
    host, buffers = env
    buffers.watch(KEY_SESSION, SID, "telegram")
    buffers.on_event(_ev(1, "user.message", {"text": "alte frage", "client_message_id": "c-1"}, id_="c-1"))
    buffers.on_event(_ev(2, "turn.start", {}, id_="t1"))
    buffers.on_event(_ev(3, "turn.done", {"final_response": "alte antwort"}, id_="t1"))
    new = "20261013_121000_ffffff"
    host.sessions = {new: {"session_key": KEY_SESSION, "session_id": new, "platform": "telegram", "live": True}}
    host.messages = []
    assert _snap(host, buffers)["messages"] == []


def test_a_compression_child_keeps_the_parents_buffered_entries(env):
    host, buffers = env
    buffers.watch(KEY_SESSION, SID, "telegram")
    buffers.on_event(_ev(1, "user.message", {"text": "frage", "client_message_id": "c-1"}, id_="c-1"))
    buffers.on_event(_ev(2, "turn.start", {}, id_="t1"))
    child = "20261013_121000_cccccc"
    host.sessions = {child: {"session_key": KEY_SESSION, "session_id": child, "platform": "telegram", "live": True}}
    host.lineages = {child: [SID, child]}
    buffers.on_event(_ev(3, "turn.done", {"final_response": "antwort"}, id_="t1", sid=child))
    assert _shown(_snap(host, buffers)) == [("c-1", "frage"), ("t1", "antwort")]


def test_a_new_session_that_reuses_the_key_never_inherits_entries_without_lineage(env):
    """Even when the plugin saw the old id in ``known_ids``, only the compression lineage counts."""
    host, buffers = env
    buffers.watch(KEY_SESSION, SID, "telegram")
    buffers.on_event(_ev(1, "user.message", {"text": "alt", "client_message_id": "c-1"}, id_="c-1"))
    new = "20261013_121000_ffffff"
    buffers.on_event(_ev(2, "user.message", {"text": "neu", "client_message_id": "c-2"}, id_="c-2", sid=new))
    host.sessions = {new: {"session_key": KEY_SESSION, "session_id": new, "platform": "telegram", "live": True}}
    assert _shown(_snap(host, buffers)) == [("c-2", "neu")]


# ── a full history window: entries older than its first row stay out ────────────────────────


def test_full_window_drops_an_answer_whose_turn_ended_before_the_window(env, monkeypatch):
    from plugins.platforms.werkbank import snapshot

    monkeypatch.setattr(snapshot, "HISTORY_LIMIT", 3)
    host, buffers = env
    buffers.watch(KEY_SESSION, SID, "telegram")
    buffers.on_event(_ev(1, "turn.start", {}, id_="t_old"))
    buffers.on_event(_ev(2, "turn.done", {"final_response": "alt"}, id_="t_old"))    # row never seen
    host.messages = [_answer(1, "alt")] + [{"id": i, "role": "tool", "content": "x", "timestamp": 1.0}
                                           for i in range(2, 6)]
    buffers.on_event(_ev(3, "turn.start", {}, id_="t_new"))
    host.messages += [{"id": i, "role": "tool", "content": "y", "timestamp": 1.0} for i in range(6, 10)]
    buffers.on_event(_ev(4, "turn.done", {"final_response": "neu"}, id_="t_new"))    # row not yet written
    assert [m["id"] for m in _snap(host, buffers)["messages"] if m["role"] == "assistant"] == ["t_new"]


# ── origin of a stored user row: only this plugin's own rows are "werkbank" ──────────────────


def test_row_origin_is_werkbank_only_for_this_plugins_rows(env):
    host, buffers = env
    buffers.watch(KEY_SESSION, SID, "telegram")
    host.messages = [
        {"id": 1, "role": "user", "content": "a", "timestamp": 1.0,
         "display_metadata": {"client_message_id": "w-1", "hermes_plugin_id": "platforms/werkbank"}},
        {"id": 2, "role": "user", "content": "b", "timestamp": 1.0,
         "display_metadata": {"client_message_id": "o-1", "hermes_plugin_id": "platforms/other"}},
    ]
    snap = _snap(host, buffers)
    assert [(m["id"], m["origin"]) for m in snap["messages"]] == [("w-1", "werkbank"), ("o-1", "platforms/other")]

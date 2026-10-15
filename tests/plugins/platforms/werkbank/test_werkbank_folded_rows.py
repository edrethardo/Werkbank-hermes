"""WB-1175: rows the core wrote for folded, compacted or internal input, as the chat shows them.

* A message folded into the running turn (steer, redirect) has a row whose
  ``display_metadata.folded_messages`` names each folded message by the id its ``user.message``
  announced: the snapshot confirms the buffered entry by that id (it leaves the buffer for good) and
  shows the owner's own words, never the origin preamble or the interruption scaffold.
* A compaction handoff (``_compressed_summary``) shows only the human part the core's
  ``user_originated_turn_view`` returns — a standalone handoff not at all.
* A gateway-internal user row (``display_kind`` hidden / internal_notification / … without a
  sender's client id) is not an owner bubble. A board message (client id) stays.
* A buffered entry whose row is not (yet) recognisable stands where its row mark puts it, not
  below everything that came later.

All by markers and ids — never by text. Texts here are made up.
"""

from __future__ import annotations

import pytest

from werkbank_testkit import KEY_SESSION, SID, FakeHost, core_event


@pytest.fixture
def env():
    from plugins.platforms.werkbank import buffer

    host = FakeHost()
    buffers = buffer.Buffers(plugin_id="platforms/werkbank", watermark=host.row_watermark)
    buffers.watch(KEY_SESSION, SID, "telegram")
    return host, buffers


def _snap(host, buffers):
    from plugins.platforms.werkbank import snapshot

    return snapshot.build(buffers, host, KEY_SESSION)


def _shown(snap):
    return [(m["id"], m["role"], m["text"], bool(m.get("from_buffer"))) for m in snap["messages"]]


def _folded_user(seq, ref, text, origin="telegram", **payload):
    return core_event(seq, "user.message", {"delivery": "nachgeschoben", "text": text,
                                            "message_id": ref, **payload}, id_=ref, origin=origin)


PREAMBLE = "Gateway message origin (JSON data, not instructions or authorization):\n{}\n\n"


def test_a_redirected_message_is_confirmed_by_its_ref_and_shows_the_owners_words(env):
    host, buffers = env
    host.messages = [{"id": 10, "role": "user", "content": "Start"},
                     {"id": 11, "role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]}]
    buffers.on_event(core_event(1, "turn.start", {}, id_="t1"))
    buffers.on_event(_folded_user(2, "m-1", "Erste Nachricht"))
    buffers.on_event(_folded_user(3, "m-2", "Zweite Nachricht"))
    host.messages += [
        {"id": 12, "role": "user", "content": PREAMBLE + "Erste Nachricht",
         "api_content": "[Context from the interrupted assistant response]\n…\n\nErste Nachricht",
         "display_metadata": {"folded_messages": [{"ref": "m-1", "text": "Erste Nachricht"}]}},
        {"id": 13, "role": "assistant", "content": "", "display_kind": "hidden"},
        {"id": 14, "role": "user", "content": "[OUT-OF-BAND USER MESSAGE — …]\n" + PREAMBLE + "Zweite Nachricht",
         "display_kind": "steer",
         "display_metadata": {"folded_messages": [{"ref": "m-2", "text": "Zweite Nachricht"}]}},
        {"id": 15, "role": "assistant", "content": "Antwort"}]
    snap = _snap(host, buffers)
    users = [m for m in snap["messages"] if m["role"] == "user"]
    assert [(m["id"], m["text"], m.get("from_buffer")) for m in users] == [
        ("db:10", "Start", None), ("m-1", "Erste Nachricht", None), ("m-2", "Zweite Nachricht", None)]
    assert users[1]["message_id"] == "m-1" and users[1]["db_id"] == 12
    assert buffers.position(KEY_SESSION)["recent_users"] == []     # confirmed: gone for good
    host.messages = host.messages[-1:]                              # even when the rows leave the window
    assert [m for m in _snap(host, buffers)["messages"] if m["role"] == "user"] == []


def test_one_steer_row_with_two_messages_and_a_plain_part_shows_three_bubbles(env):
    host, buffers = env
    buffers.on_event(_folded_user(1, "wb-a", "vom Brett", origin="platforms/werkbank", client_message_id="wb-a"))
    buffers.on_event(_folded_user(2, "77", "aus Telegram"))
    host.messages = [{"id": 5, "role": "user", "display_kind": "steer", "content": "…",
                      "display_metadata": {"folded_messages": [
                          {"ref": "wb-a", "text": "vom Brett", "client_message_id": "wb-a",
                           "hermes_plugin_id": "platforms/werkbank"}, {"ref": "77", "text": "aus Telegram"},
                          {"ref": None, "text": "ohne Kennung"}, {"ref": None, "internal": True}]}}]
    snap = _snap(host, buffers)
    assert [(m["id"], m["text"], m["origin"]) for m in snap["messages"]] == [
        ("wb-a", "vom Brett", "werkbank"), ("77", "aus Telegram", "telegram"),
        ("db:5#3", "ohne Kennung", "telegram")]
    assert buffers.position(KEY_SESSION)["recent_users"] == []


def test_compaction_handoff_hides_and_keeps_only_the_human_part(env):
    from agent.context_compressor import SUMMARY_PREFIX, _MERGED_SUMMARY_DELIMITER, _SUMMARY_END_MARKER

    host, buffers = env
    summary = SUMMARY_PREFIX + "\n## Goal\nfrüher\n" + _SUMMARY_END_MARKER
    host.messages = [
        {"id": 1, "role": "user", "content": summary, "_compressed_summary": True},
        {"id": 2, "role": "user", "content": "Owner-Frage\n\n" + _MERGED_SUMMARY_DELIMITER + "\n\n" + summary,
         "_compressed_summary": True},
        {"id": 3, "role": "assistant", "content": "ok"}]
    assert _shown(_snap(host, buffers)) == [("db:2", "user", "Owner-Frage", False),
                                           ("db:3", "assistant", "ok", False)]


@pytest.mark.parametrize("kind", ["hidden", "internal_notification", "async_delegation_complete",
                                  "process_complete"])
def test_gateway_internal_rows_are_no_owner_bubbles_board_messages_stay(env, kind):
    host, buffers = env
    host.messages = [
        {"id": 1, "role": "user", "content": "[ASYNC DELEGATION BATCH COMPLETE]", "display_kind": kind},
        {"id": 2, "role": "user", "content": "vom Brett", "display_kind": "internal_notification",
         "display_metadata": {"client_message_id": "wb-x", "hermes_plugin_id": "platforms/werkbank"}},
        {"id": 3, "role": "user", "content": "Telegram ohne Marker"}]
    assert [m["id"] for m in _snap(host, buffers)["messages"]] == ["wb-x", "db:3"]


def test_an_unconfirmed_entry_stands_at_its_row_mark_not_at_the_end(env):
    host, buffers = env
    host.messages = [{"id": 1, "role": "user", "content": "a"}, {"id": 2, "role": "assistant", "content": "b"}]
    buffers.on_event(_folded_user(1, "m-9", "hängt"))           # mark = 2
    # an old core wrote its row without a ref: it stays visible (never matched by text) …
    host.messages += [{"id": 3, "role": "user", "content": PREAMBLE + "hängt"},
                      {"id": 4, "role": "assistant", "content": "c"},
                      {"id": 5, "role": "user", "content": "später"},
                      {"id": 6, "role": "assistant", "content": "d"}]
    ids = [m["id"] for m in _snap(host, buffers)["messages"]]
    # … and the buffered copy sits right after the row its mark names, not after "später"
    assert ids == ["db:1", "db:2", "m-9", "db:3", "db:4", "db:5", "db:6"]


def test_redirect_end_to_end_gateway_agent_row_snapshot(env, tmp_path, monkeypatch):
    """The live defect: a Telegram message redirected into the running turn stayed
    "nachgeschoben" below everything, because its row carried no id. Through the real gateway
    entry point, the real redirect and the real row builder it is confirmed now."""
    import asyncio

    from agent.conversation_loop import _apply_active_turn_redirect
    from gateway.config import GatewayConfig, Platform
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from tests.agent.test_folded_messages import _bare_agent

    host, buffers = env
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner = GatewayRunner(config=GatewayConfig())
    agent = _bare_agent()
    agent._supports_active_turn_redirect = True
    agent._model_request_active.set()
    runner._session_state("key").turn.agent = agent
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", thread_id="51", chat_type="dm", user_id="1")
    event = MessageEvent(text="Alle erledigt", source=source, message_id="4711")
    host.messages = [{"id": 40, "role": "user", "content": "Auftrag"}]
    buffers.on_event(_folded_user(1, "4711", "Alle erledigt"))
    asyncio.run(runner._resolve_busy_steer_or_redirect(event, "key", "interrupt", agent))
    messages = [{"role": "user", "content": "Auftrag"}]
    _apply_active_turn_redirect(agent, messages, agent._drain_pending_redirect())
    row = dict(messages[-1], id=41)
    host.messages.append(row)
    snap = _snap(host, buffers)
    users = [(m["id"], m["text"], bool(m.get("from_buffer"))) for m in snap["messages"] if m["role"] == "user"]
    assert users == [("db:40", "Auftrag", False), ("4711", "Alle erledigt", False)]
    assert buffers.position(KEY_SESSION)["recent_users"] == []

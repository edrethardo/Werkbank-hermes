"""WB-1182: a Werkbank wake order (``werkbank_admin_wake``) is a quiet system line, not invisible.

The wake plugin injects with ``ctx.inject_message(text, origin=…)`` — no ``client_message_id``.
The core keeps the injecting plugin on the row (``display_metadata.hermes_plugin_id``) and on a
folded part. The snapshot draws such a row as ``role: system`` / ``kind: system`` with
``herkunft: <plugin id>`` — by that marker only, never by its text:

* not as the owner's bubble (``role: user``) and not left out;
* the owner's messages (board client id, Telegram) stay ``role: user``, unchanged;
* gateway-internal rows WITHOUT a plugin stay hidden (WB-1175), whatever their text says.

``role: system`` is what a board before WB-1182 already skips, so an old board stays as it was.
"""

from __future__ import annotations

import pytest

from werkbank_testkit import KEY_SESSION, SID, FakeHost, core_event

WAKE = "werkbank_admin_wake"


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
    return [(m["id"], m["role"], m.get("kind"), m.get("herkunft"), m["text"]) for m in snap["messages"]]


def test_a_wake_row_is_a_system_line_owner_rows_stay_internal_rows_stay_hidden(env):
    host, buffers = env
    host.messages = [
        {"id": 1, "role": "user", "content": "Owner fragt", "platform_message_id": "70"},
        {"id": 2, "role": "user", "content": "WB-9 wartet auf dich", "display_kind": "internal_notification",
         "display_metadata": {"hermes_plugin_id": WAKE, "gateway_input_owner": "x"}},
        {"id": 3, "role": "assistant", "content": "Schaue ich mir an."},
        {"id": 4, "role": "user", "content": "vom Brett", "display_kind": "internal_notification",
         "display_metadata": {"client_message_id": "wb-x", "hermes_plugin_id": "platforms/werkbank"}},
        # an internal row without a plugin: hidden even when its text looks like a wake order
        {"id": 5, "role": "user", "content": "WB-9 wartet auf dich", "display_kind": "internal_notification"},
        {"id": 6, "role": "user", "content": "[ASYNC DELEGATION BATCH COMPLETE]",
         "display_kind": "async_delegation_complete"}]
    assert _shown(_snap(host, buffers)) == [
        ("70", "user", None, None, "Owner fragt"),
        ("db:2", "system", "system", WAKE, "WB-9 wartet auf dich"),
        ("db:3", "assistant", None, None, "Schaue ich mir an."),
        ("wb-x", "user", None, None, "vom Brett")]


def test_a_wake_row_keeps_ids_and_origin_and_no_owner_fields(env):
    host, buffers = env
    host.messages = [{"id": 7, "role": "user", "content": "Auftrag", "display_kind": "internal_notification",
                      "display_metadata": '{"hermes_plugin_id": "werkbank_admin_wake"}', "timestamp": 12.5}]
    (item,) = _snap(host, buffers)["messages"]
    assert item["origin"] == WAKE and item["db_id"] == 7 and item["ts"] == 12.5
    assert item["client_message_id"] is None


def test_a_row_naming_a_plugin_but_not_internal_stays_the_owners(env):
    # only a gateway-internal row is a plugin's; a human row never claims one
    host, buffers = env
    host.messages = [{"id": 1, "role": "user", "content": "hallo", "platform_message_id": "71",
                      "display_metadata": {"hermes_plugin_id": WAKE}}]
    assert _shown(_snap(host, buffers))[0][:3] == ("71", "user", None)


def test_a_wake_folded_into_a_running_turn_is_a_system_line_too(env):
    host, buffers = env
    host.messages = [{"id": 10, "role": "user", "content": "Start"},
                     {"id": 12, "role": "user", "content": "Gateway message origin …",
                      "display_metadata": {"folded_messages": [
                          {"ref": "tg-1", "text": "Owner schiebt nach"},
                          {"ref": None, "text": "WB-9 wartet auf dich", "hermes_plugin_id": WAKE},
                          {"ref": None, "internal": True}]}}]
    assert _shown(_snap(host, buffers)) == [
        ("db:10", "user", None, None, "Start"),
        ("tg-1", "user", None, None, "Owner schiebt nach"),
        ("db:12#2", "system", "system", WAKE, "WB-9 wartet auf dich")]


def test_the_stream_passes_injected_by_through(env):
    host, buffers = env
    buffers.on_event(core_event(1, "user.message", {"delivery": "turn", "text": "WB-9 wartet",
                                                    "internal": True, "client_message_id": None,
                                                    "injected_by": WAKE}, origin=WAKE))
    events = list(buffers.get(KEY_SESSION).events)
    (event,) = [e for e in events if e["type"] == "user.message"]
    assert event["payload"]["injected_by"] == WAKE and event["origin"] == WAKE

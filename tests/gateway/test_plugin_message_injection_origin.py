"""WB-1182: a plugin injection names its sender even WITHOUT a ``client_message_id``.

The Werkbank wakes its admin session with ``ctx.inject_message(text, origin=…)`` (plugin
``werkbank_admin_wake``) — no client id. Since WB-1175 such a row was invisible in the Werkbank
chat (an internal row without a sender's id); before that it showed as the owner's bubble. The
core now keeps the injecting plugin on

* the SessionDB user row: ``display_metadata.hermes_plugin_id`` (via ``client_message_metadata``),
* the ``user.message`` event: ``payload.injected_by``,
* a part folded into a running turn: ``hermes_plugin_id`` with the sender's own words,

so a surface can draw it as a system line — by marker, never by text. A human message never
carries it, an internal gateway event (no plugin) neither.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway import session_events
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run_plugin_injection import client_message_metadata
from gateway.session import SessionSource

WAKE = "werkbank_admin_wake"


@pytest.fixture(autouse=True)
def _clean_bus():
    session_events._reset_for_tests()
    yield
    session_events._reset_for_tests()


def _source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="-1001", chat_type="group", user_id="12345")


def _wake_event():
    # exactly what _dispatch_plugin_origin_injection builds for ctx.inject_message(text, origin=…)
    return MessageEvent(text="Werkbank: WB-9 wartet auf dich", source=_source(), internal=True,
                        metadata={"hermes_plugin_id": WAKE, "hermes_plugin_injection": True,
                                  "gateway_session_key": "agent:main:telegram:group:-1001:12345"})


def test_injection_without_client_id_still_names_its_plugin():
    assert client_message_metadata(_wake_event()) == {"hermes_plugin_id": WAKE}


def test_no_origin_for_human_or_plugin_less_internal_events():
    human = MessageEvent(text="hi", source=_source(), message_id="777",
                         metadata={"hermes_plugin_id": "spoof", "hermes_plugin_injection": True})
    assert client_message_metadata(human) == {}              # not internal: never a plugin row
    note = MessageEvent(text="[ASYNC DELEGATION BATCH COMPLETE]", source=_source(), internal=True)
    assert client_message_metadata(note) == {}
    unflagged = MessageEvent(text="x", source=_source(), internal=True, metadata={"hermes_plugin_id": WAKE})
    assert client_message_metadata(unflagged) == {}          # only the injector's own flag counts
    empty = MessageEvent(text="x", source=_source(), internal=True,
                         metadata={"hermes_plugin_id": "", "hermes_plugin_injection": True})
    assert client_message_metadata(empty) == {}


@pytest.mark.asyncio
async def test_turn_persists_the_plugin_on_the_user_row_without_a_client_id(monkeypatch, tmp_path):
    from tests.gateway.test_plugin_injection_row_id import KEY, _runner

    runner = _runner(monkeypatch, tmp_path)
    await runner._handle_message_with_agent(_wake_event(), _source(), KEY, 1)
    meta = runner._run_agent.await_args.kwargs["persist_user_display_metadata"] or {}
    assert meta.get("hermes_plugin_id") == WAKE
    assert "client_message_id" not in meta
    assert runner._run_agent.await_args.kwargs["persist_user_display_kind"] == "internal_notification"


def test_user_message_event_names_the_injecting_plugin(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner
    from tests.gateway.test_session_events import _settle, _subscribe, _tg_event

    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    seen = _subscribe()
    runner = GatewayRunner(config=GatewayConfig())
    adapter = SimpleNamespace(_pending_messages={})
    runner._delivery_adapter_for = lambda _s: adapter
    wake = _wake_event()
    runner._queue_or_replace_pending_event("key", wake)
    runner._queue_or_replace_pending_event("other", _tg_event("vom Owner", "tg-5"))
    _settle(seen, lambda s: len(s) >= 2)
    by_text = {e["payload"]["text"]: e for e in seen if e["type"] == "user.message"}
    woke = by_text["Werkbank: WB-9 wartet auf dich"]
    assert woke["payload"]["injected_by"] == WAKE and woke["origin"] == WAKE
    assert woke["payload"]["internal"] is True and woke["payload"]["client_message_id"] is None
    assert "injected_by" not in by_text["vom Owner"]["payload"]


def test_wake_folded_into_a_running_turn_keeps_its_words_and_plugin(tmp_path, monkeypatch):
    from agent.folded_messages import folded_parts
    from gateway.run import GatewayRunner

    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner = GatewayRunner(config=GatewayConfig())
    wake = _wake_event()
    assert folded_parts(runner._steer_text_with_origin(wake.text, wake)) == [
        {"ref": None, "text": "Werkbank: WB-9 wartet auf dich", "hermes_plugin_id": WAKE}]

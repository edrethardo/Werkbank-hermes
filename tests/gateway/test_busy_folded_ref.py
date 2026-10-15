"""WB-1175: a busy steer/redirect names the message it folds into the turn.

The text ``steer()``/``redirect()`` receive is a :class:`agent.folded_messages.FoldedText` whose one
part is ``{ref, text}``: ``ref`` is the id the ``user.message`` event announces the message under
(``session_events._message_ref``: client id, else platform id) and ``text`` the owner's own words
without the origin preamble. A leftover steer that becomes the next turn keeps the parts on the
follow-up's ``persist_user_display_metadata``.
"""

from unittest.mock import AsyncMock

import pytest

from agent.folded_messages import FOLDED_MESSAGES_KEY, FoldedText, folded_parts
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


def _source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="8600000000", thread_id="51",
                         chat_type="dm", user_id="8600000000")


class Receiver:
    _supports_active_turn_redirect = True

    def __init__(self):
        self.payloads = []

    def steer(self, text):
        self.payloads.append(text)
        return True

    redirect = steer


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["steer", "interrupt"])
async def test_busy_telegram_message_folds_with_its_platform_id(mode, tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner = GatewayRunner(config=GatewayConfig())
    receiver = Receiver()
    runner._session_state("key").turn.agent = receiver
    event = MessageEvent(text="Alle todos gelöst", source=_source(), message_id="4728")
    await runner._resolve_busy_steer_or_redirect(event, "key", mode, receiver)
    (payload,) = receiver.payloads
    assert payload.startswith("Gateway message origin")      # the model-facing text is unchanged
    assert folded_parts(payload) == [{"ref": "4728", "text": "Alle todos gelöst"}]


def test_plugin_message_folds_with_its_client_id_and_internal_events_as_marker(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner = GatewayRunner(config=GatewayConfig())
    board = MessageEvent(text="von der Werkbank", source=_source(), internal=True,
                         metadata={"client_message_id": "wb-1", "hermes_plugin_injection": True,
                                   "hermes_plugin_id": "platforms/werkbank"})
    note = MessageEvent(text="[ASYNC DELEGATION BATCH COMPLETE]", source=_source(), internal=True)
    assert folded_parts(runner._steer_text_with_origin(board.text, board)) == [
        {"ref": "wb-1", "text": "von der Werkbank", "client_message_id": "wb-1",
         "hermes_plugin_id": "platforms/werkbank"}]
    assert folded_parts(runner._steer_text_with_origin(note.text, note)) == [
        {"ref": None, "internal": True}]


@pytest.mark.asyncio
async def test_leftover_steer_keeps_its_parts_on_the_follow_up_row(monkeypatch):
    from tests.gateway.test_internal_event_pin_wiring import KEY, _capture, _human_source, _make_runner
    from tests.gateway.test_pending_steer_drain import Adapter

    runner = _make_runner(monkeypatch)
    calls = []
    _capture(runner, calls)
    runner._run_agent_deliver_first_response = AsyncMock()
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._persist_prompt_pins = AsyncMock()
    runner._session_key_for_source = lambda source: KEY
    source = _human_source()
    adapter = Adapter(PlatformConfig(enabled=True), Platform.DISCORD)
    ctx = TurnContext(source=source, context_prompt="context", channel_prompt=None, session_key=KEY,
                      session_id="sess-wiring", run_generation=1, history=[])
    parts = [{"ref": "4730", "text": "Wer kümmert sich darum?"}]
    leftover = FoldedText("Gateway message origin …\n\nWer kümmert sich darum?  ", parts)

    # alone: the leftover becomes the next turn's text
    result = dict(final_response="done", messages=[], pending_steer=leftover)
    event, text = await runner._run_agent_drain_pending(result, adapter, source, KEY)
    assert event is None and folded_parts(text) == parts
    await runner._run_agent_queued_followup(ctx, adapter, text, event, result, result, None)
    assert calls[-1]["persist_user_display_metadata"][FOLDED_MESSAGES_KEY] == parts

    # behind an interrupt: the leftover waits as an event and still names its message
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value="prepared")
    result = dict(final_response="x", messages=[], interrupted=True, interrupt_message="replacement",
                  pending_steer=leftover)
    event, text = await runner._run_agent_drain_pending(result, adapter, source, KEY)
    assert text == "replacement" and event is None
    await runner._run_agent_queued_followup(ctx, adapter, text, event, result, result, None)
    assert FOLDED_MESSAGES_KEY not in (calls[-1]["persist_user_display_metadata"] or {})
    result = dict(final_response="done", messages=[])
    event, text = await runner._run_agent_drain_pending(result, adapter, source, KEY)
    assert event is not None
    await runner._run_agent_queued_followup(ctx, adapter, text, event, result, result, None)
    assert calls[-1]["persist_user_display_metadata"][FOLDED_MESSAGES_KEY] == parts

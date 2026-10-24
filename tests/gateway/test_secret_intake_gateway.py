"""WB-1212: the gateway replaces secrets at its earliest common point (``BasePlatformAdapter.handle_message``)
— before the runner, session store, transcript, Werkbank event stream or model see the text — and
offers the vault save as a button card (``send_choice_picker``) that is answered without the model.

All secrets are FAKE and assembled at runtime."""

import asyncio

import pytest

from agent import secret_intake as si
from agent.vault_store import VaultStore
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource

FAKE_GH = "ghp" + "_" + "FAKE0fake1FAKE2fake3FAKE4fake5FAKE6f"


class PickerAdapter(BasePlatformAdapter):
    def __init__(self, *, with_picker=True):
        super().__init__(PlatformConfig(enabled=True, token="fake-token"), Platform.TELEGRAM)
        self._busy_text_mode = ""
        self.sent, self.pickers = [], []
        if not with_picker:
            self.send_choice_picker = None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append({"chat_id": chat_id, "content": content, "metadata": metadata})
        return SendResult(success=True, message_id="1")

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}

    async def send_choice_picker(self, chat_id, title, choices, session_key, on_choice_selected, metadata=None):
        self.pickers.append({"chat_id": chat_id, "title": title, "choices": choices,
                             "session_key": session_key, "cb": on_choice_selected})
        return SendResult(success=True, message_id="2")


def _event(text, *, reply_to_text=None):
    return MessageEvent(
        text=text, reply_to_text=reply_to_text,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="4711", chat_type="dm", user_id="42"),
        message_id="m1")


@pytest.fixture(autouse=True)
def _intake_on(monkeypatch):
    si.store().clear()
    monkeypatch.setattr(si, "enabled", lambda: True)
    yield
    si.store().clear()


async def _run(adapter, event):
    seen = []

    async def handler(ev):
        seen.append((ev.text, ev.reply_to_text))
        return None

    adapter.set_message_handler(handler)
    await adapter.handle_message(event)
    for _ in range(100):
        if seen and adapter.pickers or (seen and not hasattr(adapter, "pickers")):
            break
        await asyncio.sleep(0.01)
    for _ in range(20):
        await asyncio.sleep(0.01)
    return seen


@pytest.mark.asyncio
async def test_runner_sees_only_the_placeholder_rest_unchanged():
    adapter = PickerAdapter()
    seen = await _run(adapter, _event(f"Hier: {FAKE_GH} — bitte einrichten.\n"))
    assert seen and seen[0][0] == "Hier: [GEHEIMNIS:github-token-1] — bitte einrichten.\n"


@pytest.mark.asyncio
async def test_quoted_reply_text_is_redacted_too():
    adapter = PickerAdapter()
    seen = await _run(adapter, _event("was war das?", reply_to_text=f"alt {FAKE_GH}"))
    assert seen[0][1] == "alt [GEHEIMNIS:github-token-1]"


@pytest.mark.asyncio
async def test_card_offers_save_and_saving_needs_no_model():
    from hermes_constants import get_hermes_home
    adapter = PickerAdapter()
    await _run(adapter, _event(f"token {FAKE_GH}"))
    assert len(adapter.pickers) == 1
    card = adapter.pickers[0]
    assert "[GEHEIMNIS:github-token-1]" in card["title"] and FAKE_GH not in card["title"]
    labels = [c["label"] for c in card["choices"]]
    assert any("Tresor" in label for label in labels) and any("Verwerfen" in label for label in labels)
    save = next(c["value"] for c in card["choices"] if c["value"].startswith("save:"))
    result = await card["cb"]("4711", save)
    assert "gespeichert" in result and FAKE_GH not in result
    items = VaultStore(get_hermes_home() / "vault").list_items()
    assert [i.kind for i in items] == ["secret"]
    assert VaultStore(get_hermes_home() / "vault").resolve_secret(items[0].id) == {"value": FAKE_GH}


@pytest.mark.asyncio
async def test_discard_from_the_card():
    adapter = PickerAdapter()
    await _run(adapter, _event(f"token {FAKE_GH}"))
    card = adapter.pickers[0]
    drop = next(c["value"] for c in card["choices"] if c["value"].startswith("drop:"))
    assert "verworfen" in await card["cb"]("4711", drop)
    assert all(e.value is None for e in si.store()._by_id.values())


@pytest.mark.asyncio
async def test_without_picker_a_text_notice_without_value():
    adapter = PickerAdapter(with_picker=False)
    seen = await _run(adapter, _event(f"token {FAKE_GH}"))
    assert seen[0][0] == "token [GEHEIMNIS:github-token-1]"
    notices = [s["content"] for s in adapter.sent if "GEHEIMNIS" in s["content"]]
    assert notices and all(FAKE_GH not in n for n in notices)


@pytest.mark.asyncio
async def test_switch_off_passes_text_through(monkeypatch):
    monkeypatch.setattr(si, "enabled", lambda: False)
    adapter = PickerAdapter()
    seen = await _run(adapter, _event(f"token {FAKE_GH}"))
    assert seen[0][0] == f"token {FAKE_GH}" and adapter.pickers == []


@pytest.mark.asyncio
async def test_already_redacted_message_gets_its_card_once():
    """The Werkbank plugin redacts before its mirror and injects placeholders; the gateway offers."""
    adapter = PickerAdapter()
    key = adapter._event_session_key(_event("x"))
    pre = si.intake(f"token {FAKE_GH}", key, surface="werkbank").text
    seen = await _run(adapter, _event(pre))
    assert seen[0][0] == "token [GEHEIMNIS:github-token-1]"
    assert len(adapter.pickers) == 1
    await _run(adapter, _event(pre))
    assert len(adapter.pickers) == 1


# -- STT transcripts / document notes / edits / picker (WB-1212 nachbesserung) ---------------------

def _stt_runner(adapter):
    from types import SimpleNamespace
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(stt_enabled=True, group_sessions_per_user=True,
                                    thread_sessions_per_user=False)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._consume_pending_native_image_paths = lambda _key: []
    runner._session_key_for_source = lambda _source: "telegram:dm:4711"
    runner._thread_metadata_for_source = lambda *_a, **_k: {}
    runner._reply_anchor_for_event = lambda _event: None
    runner._should_echo_stt_transcripts = lambda: True
    runner._delivery_adapter_for = lambda _source: adapter
    return runner


def _voice_event():
    from gateway.platforms.event import MessageType
    return MessageEvent(text="", message_type=MessageType.VOICE, message_id="v1",
                        source=SessionSource(platform=Platform.TELEGRAM, chat_id="4711", chat_type="dm", user_id="42"),
                        media_urls=["/tmp/fake-voice.ogg"], media_types=["audio/ogg"])


@pytest.mark.asyncio
async def test_stt_transcript_is_redacted_before_echo_and_agent_text():
    from unittest.mock import patch
    adapter = PickerAdapter()
    runner = _stt_runner(adapter)
    event = _voice_event()
    with patch("tools.transcription_tools.transcribe_audio",
               return_value={"success": True, "transcript": f"mein token {FAKE_GH}", "provider": "mock"}), \
         patch("gateway.run_inbound.rehome_inbound_media", lambda ev: None):
        text = await runner._prepare_inbound_message_text(event=event, source=event.source, history=[],
                                                          session_key="telegram:dm:4711")
    for _ in range(20):
        await asyncio.sleep(0.01)
    assert FAKE_GH not in text and "[GEHEIMNIS:github-token-1]" in text
    echoes = [s["content"] for s in adapter.sent]
    assert echoes and all(FAKE_GH not in e for e in echoes)
    assert any("[GEHEIMNIS:github-token-1]" in e for e in echoes)
    assert len(adapter.pickers) == 1 and FAKE_GH not in adapter.pickers[0]["title"]


@pytest.mark.asyncio
async def test_pending_voice_cache_holds_only_the_placeholder():
    from unittest.mock import patch
    adapter = PickerAdapter()
    runner = _stt_runner(adapter)
    event = _voice_event()
    with patch("tools.transcription_tools.transcribe_audio",
               return_value={"success": True, "transcript": f"key {FAKE_GH}", "provider": "mock"}):
        text, transcripts = await runner._transcribe_pending_audio_event_once(event, "")
    assert FAKE_GH not in text and FAKE_GH not in event._gateway_pending_stt_text
    assert all(FAKE_GH not in t for t in transcripts + event._gateway_pending_stt_transcripts)


@pytest.mark.asyncio
async def test_prepared_text_redacts_document_notes_and_late_additions():
    adapter = PickerAdapter()
    runner = _stt_runner(adapter)
    event = _event("bitte ansehen")
    runner._prepend_inbound_document_notes = lambda ev, text: f"[Anhang: notes.txt] token {FAKE_GH}\n\n{text}"
    text = await runner._prepare_inbound_message_text(event=event, source=event.source, history=[],
                                                      session_key="telegram:dm:4711")
    assert FAKE_GH not in text and text.endswith("bitte ansehen")


@pytest.mark.asyncio
async def test_edited_event_text_reaches_platform_hooks_redacted():
    from unittest.mock import MagicMock, patch
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner._is_user_authorized_for_source = lambda source: True
    runner._session_key_for_source = lambda source: "telegram:dm:4711"
    invoke = MagicMock()
    event = {"platform": "telegram", "event_type": "message_edited",
             "payload": {"chat_id": "4711", "message_id": "9", "text": f"neu {FAKE_GH}"}}
    with patch("hermes_cli.lifecycle.has_hook", lambda _n: True), patch("hermes_cli.lifecycle.invoke_hook", invoke):
        await runner._handle_gateway_platform_event(event, _event("x").source)
    sent = invoke.call_args.kwargs
    assert sent["payload"]["text"] == "neu [GEHEIMNIS:github-token-1]"
    assert FAKE_GH not in str(sent)


@pytest.mark.asyncio
async def test_telegram_picker_tap_on_an_old_card_does_not_apply_the_new_one():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from plugins.platforms.telegram.adapter import TelegramAdapter
    a = object.__new__(TelegramAdapter)
    calls = []

    async def cb(chat_id, value):
        calls.append(value)
        return "ok"

    a._choice_picker_state = {"4711": {"msg_id": 200, "choices": [{"value": "save:new"}],
                                       "session_key": "k", "on_choice_selected": cb}}
    a._edit_result_text = AsyncMock()
    old = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(message_id=100))
    await a._handle_choice_picker_callback(old, "cp:0", "4711")
    assert calls == [] and "4711" in a._choice_picker_state
    new = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(message_id=200))
    await a._handle_choice_picker_callback(new, "cp:0", "4711")
    assert calls == ["save:new"]


# -- WB-1261: third button „Kein Geheimnis“ ------------------------------------------------------

FAKE_BARE = "Zq8" + "FAKEk2Lm9Xp4Rt7Wv1Bn6Hj3Cs5Df8Gy0Qa4Ne"


class ReleaseAdapter(PickerAdapter):
    """An adapter whose picker reports who tapped (Telegram passes ``user_id``)."""
    choice_picker_reports_user = True


async def _card_for(adapter, text):
    si._RELEASED.clear()
    await _run(adapter, _event(text))
    assert len(adapter.pickers) == 1
    card = adapter.pickers[0]
    return card, next((c["value"] for c in card["choices"] if c["value"].startswith("keep:")), None)


async def _drain():
    for _ in range(30):
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_kein_geheimnis_injects_the_original_into_the_session_and_saves_nothing(monkeypatch):
    from hermes_constants import get_hermes_home
    adapter = ReleaseAdapter()
    card, keep = await _card_for(adapter, f"nimm {FAKE_BARE} bitte")
    assert keep and any("Kein Geheimnis" in c["label"] for c in card["choices"])
    admitted = []

    async def fake_admit(ad, ev):
        admitted.append((ad, ev))
    monkeypatch.setattr("gateway.wake.admit_internal_event", fake_admit)
    result = await card["cb"]("4711", keep, user_id="42")
    await _drain()
    assert "Kein Geheimnis" in result and FAKE_BARE not in result
    assert len(admitted) == 1
    ad, ev = admitted[0]
    assert ad is adapter and ev.internal is True
    assert "Freigegeben" in ev.text and "[GEHEIMNIS:api-key-1]" in ev.text and FAKE_BARE in ev.text
    assert ev.source.chat_id == "4711" and ev.source.user_id == "42"
    assert (ev.metadata or {}).get("gateway_session_key") == card["session_key"]
    assert not (get_hermes_home() / "vault").exists() or VaultStore(get_hermes_home() / "vault").list_items() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", ["99", None])
async def test_kein_geheimnis_from_a_stranger_is_refused(monkeypatch, user_id):
    adapter = ReleaseAdapter()
    card, keep = await _card_for(adapter, f"nimm {FAKE_BARE} bitte")
    admitted = []

    async def fake_admit(ad, ev):
        admitted.append(ev)
    monkeypatch.setattr("gateway.wake.admit_internal_event", fake_admit)
    result = await card["cb"]("4711", keep, user_id=user_id)
    await _drain()
    assert admitted == [] and FAKE_BARE not in result and "⛔" in result
    assert [e.status for e in si.store()._by_id.values()] == ["pending"]


@pytest.mark.asyncio
async def test_picker_without_user_report_offers_no_third_button():
    adapter = PickerAdapter()
    card, keep = await _card_for(adapter, f"nimm {FAKE_BARE} bitte")
    assert keep is None and len(card["choices"]) == 2


@pytest.mark.asyncio
async def test_telegram_picker_passes_the_tapping_user_to_a_card_callback():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from plugins.platforms.telegram.adapter import TelegramAdapter
    assert TelegramAdapter.choice_picker_reports_user is True
    a = object.__new__(TelegramAdapter)
    calls = []

    async def cb(chat_id, value, user_id=None):
        calls.append((value, user_id))
        return "ok"

    a._choice_picker_state = {"4711": {"msg_id": 200, "choices": [{"value": "keep:x"}],
                                       "session_key": "k", "on_choice_selected": cb}}
    a._edit_result_text = AsyncMock()
    q = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(message_id=200),
                        from_user=SimpleNamespace(id=42))
    await a._handle_choice_picker_callback(q, "cp:0", "4711")
    assert calls == [("keep:x", "42")]

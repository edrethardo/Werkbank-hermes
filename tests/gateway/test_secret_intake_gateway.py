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

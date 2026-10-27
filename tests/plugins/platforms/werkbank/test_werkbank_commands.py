"""WB-1256: slash commands from the board chat, and the choice cards they open.

``POST /v1/s/{key}/messages`` with ``/status`` goes to the gateway as a COMMAND
(``inject_message(…, as_command=True)``), never as text for the model, and without the Telegram
mirror; ``command.reply`` / ``choice.request`` come back through the stream and the snapshot; a
card is answered with ``POST /v1/s/{key}/choices/{choice_id} {index}`` — only a card of THAT session.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from werkbank_testkit import KEY_SESSION, SID, FakeHost, call, core_event, running_server, session_path

from gateway import plugin_commands
from plugins.platforms.werkbank import commands as commands_mod

OTHER_KEY = "agent:main:telegram:dm:999:7"


@pytest.fixture(autouse=True)
def _clean_cards():
    plugin_commands._reset_for_tests()
    yield
    plugin_commands._reset_for_tests()


class CommandHost(FakeHost):
    def __init__(self, *, commands=True):
        super().__init__()
        self.supports_commands = commands
        self.as_command: list = []

    def pending_choices(self, session_key):
        return commands_mod.pending_choices(session_key)        # the real core registry

    def inject(self, session_key, text, client_message_id, reply_to_message_id=None, **kw):
        self.as_command.append(bool(kw.get("command")))
        return super().inject(session_key, text, client_message_id, reply_to_message_id, **kw)


@pytest.fixture
def srv(tmp_path):
    s = running_server(tmp_path, CommandHost())
    assert call(s.port, "GET", f"/v1/resolve?session_id={SID}", s.key)[0] == 200
    yield s
    s.server.stop()


def send(srv, text, cmid, **extra):
    return call(srv.port, "POST", session_path("messages"), srv.key,
                {"text": text, "client_message_id": cmid, **extra})


# ── what counts as a command: the core's own rule (MessageEvent.get_command) ─────────────────


@pytest.mark.parametrize("text, expected", [
    ("/status", True), ("  /model gpt-5.5", True), ("/new@HermesBot", True), ("/Reasoning high", True),
    ("/home/aaron/datei ist kaputt", False), ("/", False), ("hallo /status", False), ("", False),
])
def test_command_rule_matches_the_gateway(text, expected):
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource
    from gateway.config import Platform
    core = bool(MessageEvent(text=text, source=SessionSource(platform=Platform.TELEGRAM, chat_id="1")).get_command())
    assert commands_mod.is_command(text) is expected is core


# ── sending ─────────────────────────────────────────────────────────────────────────────────


def test_a_command_goes_as_a_command_without_the_mirror(srv):
    assert send(srv, "/status", "wb-1") == (200, {"accepted": True})
    assert srv.host.injected == [(KEY_SESSION, "/status", "wb-1")]
    assert srv.host.as_command == [True]
    assert srv.host.mirrored == []                      # the Telegram topic gets no "/status" line


def test_plain_text_and_a_path_stay_conversation(srv):
    assert send(srv, "hallo", "wb-1")[0] == 200
    assert send(srv, "/home/aaron/x geht nicht", "wb-2")[0] == 200
    assert srv.host.as_command == [False, False]
    assert len(srv.host.mirrored) == 2


def test_a_core_without_commands_refuses_instead_of_sending_text(tmp_path):
    s = running_server(tmp_path, CommandHost(commands=False))
    try:
        assert call(s.port, "GET", f"/v1/resolve?session_id={SID}", s.key)[0] == 200
        status, answer = send(s, "/status", "wb-1")
        assert status == 501 and answer["accepted"] is False and "Befehl" in answer["grund"]
        assert s.host.injected == [] and s.host.mirrored == []
        assert send(s, "/status", "wb-1")[0] == 501      # the id was released: no 409 on a retry
    finally:
        s.server.stop()


def test_a_command_with_attachments_is_refused(srv):
    status, answer = send(srv, "/status", "wb-1", attachments=["up_aaaaaaaaaaaaaaaa"])
    assert status == 400 and srv.host.injected == []


def test_an_unresolved_session_cannot_send_a_command(srv):
    status, _ = call(srv.port, "POST", session_path("messages", key=OTHER_KEY), srv.key,
                     {"text": "/new", "client_message_id": "wb-9"})
    assert status == 404 and srv.host.injected == []


# ── the reply: stream and snapshot ───────────────────────────────────────────────────────────


def test_command_reply_reaches_the_snapshot_as_a_system_line(srv):
    srv.host.messages = [{"id": 1, "role": "user", "content": "hallo", "timestamp": 1.0}]
    srv.buffers.on_event(core_event(1, "command.reply", {"command": "status", "text": "📊 Status",
                                                          "client_message_id": "wb-1"},
                                    id_="wb-1", origin="platforms/werkbank"))
    srv.buffers.on_event(core_event(2, "command.reply", {"command": "status", "text": "alt"},
                                    id_="wb-0", sid="frueher", origin="platforms/werkbank"))
    status, snap = call(srv.port, "GET", session_path("snapshot"), srv.key)
    assert status == 200
    system = [m for m in snap["messages"] if m["role"] == "system"]
    assert [(m["herkunft"], m["text"], m["id"]) for m in system] == [("befehl", "📊 Status", "cmd:wb-1")]
    assert system[0]["command"] == "status"


# ── choice cards ─────────────────────────────────────────────────────────────────────────────


class _Loop:
    """The gateway loop in its own thread (the plugin answers from its HTTP thread)."""

    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        return self

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(5)

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


def open_card(loop, key, picked):
    async def on_choice(_chat, value):
        picked.append(value)
        return f"Reasoning: {value}"

    surface = plugin_commands.ChoiceSurface(key, "platforms/werkbank", "wb-1", "telegram")
    result = loop.run(surface.send_choice_picker(
        chat_id="c", title="Reasoning?", session_key=key, on_choice_selected=on_choice,
        choices=[{"value": "low", "label": "niedrig"}, {"value": "high", "label": "hoch"}]))
    return result.message_id


def test_a_card_is_answered_by_index_and_closes(srv):
    picked = []
    with _Loop() as loop:
        cid = open_card(loop, KEY_SESSION, picked)
        status, snap = call(srv.port, "GET", session_path("snapshot"), srv.key)
        assert [c["choice_id"] for c in snap["pending_choices"]] == [cid]
        status, answer = call(srv.port, "POST", session_path("choices", cid), srv.key, {"index": 1})
        assert status == 200 and answer["resolved"] is True and answer["text"] == "Reasoning: high"
        assert picked == ["high"]
        assert call(srv.port, "POST", session_path("choices", cid), srv.key, {"index": 1})[0] == 404
        assert call(srv.port, "GET", session_path("snapshot"), srv.key)[1]["pending_choices"] == []


def test_a_card_of_another_session_is_not_answered_here(srv):
    picked = []
    with _Loop() as loop:
        cid = open_card(loop, OTHER_KEY, picked)
        status, _ = call(srv.port, "POST", session_path("choices", cid), srv.key, {"index": 0})
        assert status == 404 and picked == []
        assert [c["choice_id"] for c in plugin_commands.pending(OTHER_KEY)] == [cid]   # still open


@pytest.mark.parametrize("body", [{}, {"index": "1"}, {"index": True}, {"index": -1}, {"index": 7}])
def test_a_bad_index_picks_nothing(srv, body):
    picked = []
    with _Loop() as loop:
        cid = open_card(loop, KEY_SESSION, picked)
        assert call(srv.port, "POST", session_path("choices", cid), srv.key, body)[0] == 400
        assert picked == [] and plugin_commands.pending(KEY_SESSION)


def test_a_bad_choice_id_is_400(srv):
    assert call(srv.port, "POST", session_path("choices", "../x"), srv.key, {"index": 0})[0] in (400, 404)


# ── the command list for the board's completion ───────────────────────────────────────────────


def test_command_list_comes_from_the_gateway_registry(srv):
    status, answer = call(srv.port, "GET", "/v1/commands", srv.key)
    assert status == 200
    names = {c["name"] for c in answer["commands"]}
    assert {"status", "model", "new", "stop", "reasoning", "help"} <= names
    model = next(c for c in answer["commands"] if c["name"] == "model")
    assert model["description"]
    assert call(srv.port, "GET", "/v1/commands")[0] == 401

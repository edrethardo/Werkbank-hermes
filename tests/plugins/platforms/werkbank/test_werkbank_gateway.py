"""WB-1128 against the real gateway pieces: the core event bus (``gateway/session_events``), the
gateway busy path, a real SessionDB, the WB-1127 plugin context interface (and a pre-WB-1127 one)."""

from __future__ import annotations

import asyncio
import inspect
import logging
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import session_events
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from werkbank_testkit import KEY_SESSION, SID, call, events_path, session_path, sse

PLUGIN_ID = "platforms/werkbank"
HOOK = "on_gateway_session_event"


@pytest.fixture(autouse=True)
def _clean_bus():
    session_events._reset_for_tests()
    yield
    session_events._reset_for_tests()


def _source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="4711", chat_type="dm", user_id="4711",
                         thread_id="51")


class FakeStore:
    def __init__(self):
        self.entry = SimpleNamespace(session_key=KEY_SESSION, session_id=SID, origin=_source(),
                                     platform=Platform.TELEGRAM)

    def lookup_by_session_id(self, session_id):
        return self.entry if session_id == self.entry.session_id else None

    def lookup_by_session_key(self, key):
        return self.entry if key == KEY_SESSION else None

    def peek_session_id(self, key):
        return self.entry.session_id if key == KEY_SESSION else None


class FakeAdapter:
    def __init__(self):
        self.sent = []
        self.deleted = []
        self.answer = SimpleNamespace(success=True, message_id="900")

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content, metadata))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True


class FakeOwner:
    def __init__(self, db=None):
        self.session_store = FakeStore()
        self._session_db = db
        self._running = True
        self._running_agents = {}
        self.adapter = FakeAdapter()
        self._gateway_loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._gateway_loop.run_forever, daemon=True)
        self._thread.start()
        self.config = GatewayConfig()

    def _restored_source(self, entry):
        return entry.origin

    def _delivery_adapter_for(self, source):
        return self.adapter

    def close(self):
        self._gateway_loop.call_soon_threadsafe(self._gateway_loop.stop)
        self._thread.join(2)


class Ctx127:
    """The plugin context after WB-1127: inject_message(client_message_id=), interrupt_session."""

    plugin_id = PLUGIN_ID

    def __init__(self, owner):
        self._manager = SimpleNamespace(_gateway_message_injector=(owner, None), home_path=None)
        self.injected = []
        self.interrupted = []
        self.hooks = {}
        self.unload = []

    def inject_message(self, content, role="user", *, session_key=None, origin=None, client_message_id=None):
        self.injected.append({"text": content, "session_key": session_key,
                              "client_message_id": client_message_id})
        return True

    def interrupt_session(self, session_key):
        self.interrupted.append(session_key)
        return True

    def register_hook(self, name, callback):
        self.hooks.setdefault(name, []).append(callback)

    def on_unload(self, callback):
        self.unload.append(callback)


class CtxReply(Ctx127):
    """WB-1148: inject_message(reply_to_message_id=) anchors the turn's answer on a message."""

    def __init__(self, owner, accept=True):
        super().__init__(owner)
        self.accept = accept

    def inject_message(self, content, role="user", *, session_key=None, origin=None, client_message_id=None,
                       reply_to_message_id=None):
        self.injected.append({"text": content, "session_key": session_key,
                              "client_message_id": client_message_id, "reply_to_message_id": reply_to_message_id})
        return self.accept


class CtxOld(Ctx127):
    """Before WB-1127: no client_message_id, no interrupt_session."""

    def inject_message(self, content, role="user", *, session_key=None, origin=None):
        self.injected.append({"text": content, "session_key": session_key, "client_message_id": None})
        return True

    interrupt_session = None


def _injected_event(call_):
    """The MessageEvent ``_dispatch_plugin_message_injection`` builds for an injected message."""
    meta = {"hermes_plugin_id": PLUGIN_ID, "hermes_plugin_injection": True, "gateway_session_key": KEY_SESSION,
            "gateway_session_id": SID, "gateway_session_strict": True}
    if call_["client_message_id"]:
        meta["client_message_id"] = call_["client_message_id"]
    event = MessageEvent(text=call_["text"], source=_source(), internal=True, metadata=meta)
    return event


def _start(tmp_path, ctx, *, mirror=True, mirror_name=""):
    from plugins.platforms.werkbank import buffer, keyfile, server
    from plugins.platforms.werkbank.host import GatewayHost

    key = keyfile.ensure_key(tmp_path)
    host = GatewayHost(ctx, tmp_path)
    buffers = buffer.Buffers(plugin_id=PLUGIN_ID, watermark=host.row_watermark)
    srv = server.WerkbankServer(key, buffers, host, port=0, heartbeat=0.1, mirror_enabled=lambda: mirror,
                                mirror_name=mirror_name).start()
    return SimpleNamespace(server=srv, key=key, buffers=buffers, host=host, port=srv.port)


def _subscribe(buffers):
    """Register the plugin's hook on the active plugin manager, as ``ctx.register_hook`` does."""
    from hermes_cli.plugins import get_plugin_manager

    def on_gateway_session_event(event=None, **_kw):
        buffers.on_event(event)

    manager = get_plugin_manager()
    manager._discovered = True
    manager._hooks.setdefault(HOOK, []).append(on_gateway_session_event)
    return on_gateway_session_event


def _settle(pred, timeout=5.0):
    assert session_events.wait_idle(timeout)
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pred()


# ── the real interface of WB-1127 is detected ──────────────────────────────────────────────


def test_real_plugin_context_offers_client_message_id_and_interrupt_session():
    from hermes_cli.plugins import PluginContext
    from plugins.platforms.werkbank.host import accepts_kwarg

    assert accepts_kwarg(PluginContext.inject_message, "client_message_id")
    assert callable(getattr(PluginContext, "interrupt_session", None))


# ── a message during a running turn is steered and carries its client_message_id ───────────


class _Receiver:
    _supports_active_turn_redirect = True

    def __init__(self):
        self.payloads = []

    def steer(self, text):
        self.payloads.append(text)
        return True

    redirect = steer


@pytest.mark.asyncio
@pytest.mark.parametrize("ctx_class", [Ctx127, CtxOld])
async def test_message_during_a_turn_is_nachgeschoben_with_its_client_message_id(tmp_path, monkeypatch,
                                                                                  ctx_class):
    from gateway.run import GatewayRunner

    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    owner = FakeOwner()
    ctx = ctx_class(owner)
    srv = _start(tmp_path, ctx)
    try:
        _subscribe(srv.buffers)
        assert call(srv.port, "GET", f"/v1/resolve?session_id={SID}", srv.key)[0] == 200
        epoch = srv.buffers.get(KEY_SESSION).epoch

        runner = GatewayRunner(config=GatewayConfig())
        receiver = _Receiver()
        runner._session_state(KEY_SESSION).turn.agent = receiver
        session_events.begin_turn(SimpleNamespace(session_key=KEY_SESSION, session_id=SID, source=_source(),
                                                  inbound_message_id="tg-1", run_generation=1))
        status, answer = await asyncio.to_thread(
            call, srv.port, "POST", session_path("messages"), srv.key,
            {"text": "und die Logs auch", "client_message_id": "wb-77"})
        assert (status, answer) == (200, {"accepted": True})
        assert ctx.injected[-1]["session_key"] == KEY_SESSION
        await runner._resolve_busy_steer_or_redirect(_injected_event(ctx.injected[-1]), KEY_SESSION, "steer",
                                                     receiver)
        _settle(lambda: srv.buffers.get(KEY_SESSION).seq >= 2)

        status, events = await asyncio.to_thread(sse, srv.port, srv.key, events_path(epoch=epoch), 2)
        assert [e["type"] for e in events] == ["turn.start", "user.message"]
        message = events[1]
        assert message["payload"]["delivery"] == "nachgeschoben"
        assert message["payload"]["client_message_id"] == "wb-77" and message["id"] == "wb-77"
        assert message["origin"] == "werkbank"
        assert len(receiver.payloads) == 1 and receiver.payloads[0].endswith("und die Logs auch")
        assert ctx.injected[-1]["client_message_id"] == ("wb-77" if ctx_class is Ctx127 else None)
    finally:
        srv.server.stop()
        owner.close()


def test_stop_uses_the_real_interface_or_answers_501(tmp_path):
    for ctx_class, expected in ((Ctx127, (200, {"stopped": True})), (CtxOld, 501)):
        owner = FakeOwner()
        ctx = ctx_class(owner)
        srv = _start(tmp_path / ctx_class.__name__, ctx)
        try:
            call(srv.port, "GET", f"/v1/resolve?session_id={SID}", srv.key)
            status, answer = call(srv.port, "POST", session_path("stop"), srv.key, {})
            if expected == 501:
                assert status == 501 and "interrupt_session" in answer["grund"]
            else:
                assert (status, answer) == expected and ctx.interrupted == [KEY_SESSION]
        finally:
            srv.server.stop()
            owner.close()


# ── mirror line into the Telegram topic ───────────────────────────────────────────────────────


@pytest.mark.parametrize("mirror", [True, False])
def test_werkbank_message_is_mirrored_into_the_topic_when_switched_on(tmp_path, mirror):
    from plugins.platforms.werkbank.mirror import TELEGRAM_PREFORMATTED

    owner = FakeOwner()
    srv = _start(tmp_path, Ctx127(owner), mirror=mirror)
    try:
        call(srv.port, "GET", f"/v1/resolve?session_id={SID}", srv.key)
        call(srv.port, "POST", session_path("messages"), srv.key, {"text": "Deploy jetzt?", "client_message_id": "m1"})
        assert owner.adapter.sent == ([("4711", "👤 *Du · Werkbank*\n>Deploy jetzt?",
                                        {"thread_id": "51", TELEGRAM_PREFORMATTED: True})]
                                      if mirror else [])
    finally:
        srv.server.stop()
        owner.close()


# ── WB-1148: Hermes' answer replies to the mirrored message ───────────────────────────────────


def _post(srv, text="Deploy jetzt?", cmid="m1"):
    call(srv.port, "GET", f"/v1/resolve?session_id={SID}", srv.key)
    return call(srv.port, "POST", session_path("messages"), srv.key, {"text": text, "client_message_id": cmid})


def test_the_mirror_goes_out_first_and_its_message_id_anchors_the_answer(tmp_path):
    owner = FakeOwner()
    ctx = CtxReply(owner)
    srv = _start(tmp_path, ctx, mirror_name="Aaron")
    try:
        assert _post(srv) == (200, {"accepted": True})
        assert owner.adapter.sent[0][1] == "👤 *Aaron · Werkbank*\n>Deploy jetzt?"
        assert ctx.injected == [{"text": "Deploy jetzt?", "session_key": KEY_SESSION, "client_message_id": "m1",
                                 "reply_to_message_id": "900"}]
        assert owner.adapter.deleted == []
    finally:
        srv.server.stop()
        owner.close()


@pytest.mark.parametrize("answer", [SimpleNamespace(success=False, message_id=None, error="flood"),
                                    SimpleNamespace(success=True, message_id=None),
                                    RuntimeError("telegram down")])
def test_a_failed_mirror_leaves_the_answer_without_a_reply_anchor(tmp_path, answer):
    owner = FakeOwner()
    owner.adapter.answer = answer
    ctx = CtxReply(owner)
    srv = _start(tmp_path, ctx)
    try:
        assert _post(srv) == (200, {"accepted": True})
        assert ctx.injected[0]["reply_to_message_id"] is None
    finally:
        srv.server.stop()
        owner.close()


def test_mirror_switched_off_sends_nothing_and_anchors_nothing(tmp_path):
    owner = FakeOwner()
    ctx = CtxReply(owner)
    srv = _start(tmp_path, ctx, mirror=False)
    try:
        assert _post(srv) == (200, {"accepted": True})
        assert owner.adapter.sent == [] and ctx.injected[0]["reply_to_message_id"] is None
    finally:
        srv.server.stop()
        owner.close()


def test_a_slow_mirror_does_not_hold_the_message(tmp_path, monkeypatch):
    from plugins.platforms.werkbank import host as host_mod

    monkeypatch.setattr(host_mod, "MIRROR_WAIT_S", 0.2)
    owner = FakeOwner()
    release = threading.Event()
    original = owner.adapter.send

    async def slow(chat_id, content, reply_to=None, metadata=None):
        await asyncio.get_running_loop().run_in_executor(None, release.wait, 3)
        return await original(chat_id, content, reply_to, metadata)

    owner.adapter.send = slow
    ctx = CtxReply(owner)
    srv = _start(tmp_path, ctx)
    try:
        started = time.monotonic()
        assert _post(srv) == (200, {"accepted": True})
        assert time.monotonic() - started < 2
        assert ctx.injected[0]["reply_to_message_id"] is None
    finally:
        release.set()
        srv.server.stop()
        owner.close()


def test_a_refused_message_takes_its_mirror_back(tmp_path):
    owner = FakeOwner()
    srv = _start(tmp_path, CtxReply(owner, accept=False))
    try:
        status, _ = _post(srv)
        assert status == 503
        deadline = time.monotonic() + 3
        while not owner.adapter.deleted and time.monotonic() < deadline:
            time.sleep(0.02)
        assert owner.adapter.deleted == [("4711", "900")]
    finally:
        srv.server.stop()
        owner.close()


def test_an_older_core_without_the_anchor_kwarg_still_gets_the_message(tmp_path):
    owner = FakeOwner()
    ctx = Ctx127(owner)                     # inject_message has no reply_to_message_id
    srv = _start(tmp_path, ctx)
    try:
        assert _post(srv) == (200, {"accepted": True})
        assert ctx.injected == [{"text": "Deploy jetzt?", "session_key": KEY_SESSION, "client_message_id": "m1"}]
    finally:
        srv.server.stop()
        owner.close()


def _telegram_origin(username="aaron_handle", full_name="Aaron Beispiel"):
    """The origin the real Telegram adapter builds: ``user_name`` is the @-username when there is one."""
    from tests.gateway.conftest import _ensure_telegram_mock

    _ensure_telegram_mock()
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="t", extra={}))
    message = SimpleNamespace(from_user=SimpleNamespace(id=4711, is_bot=False, username=username, full_name=full_name),
                              chat=SimpleNamespace(id=4711, type="private", is_forum=False),
                              message_thread_id=None, is_topic_message=False, sender_chat=None)
    source = adapter._source_from_message_for_auth(message)
    assert source.user_name == username                   # the @-name, not "Aaron"
    return source


@pytest.mark.parametrize("configured,header", [("", "👤 *Du · Werkbank*\n"), ("Aaron", "👤 *Aaron · Werkbank*\n")])
def test_without_mirror_name_the_header_says_du_never_the_telegram_username(tmp_path, configured, header):
    owner = FakeOwner()
    owner.session_store.entry.origin = _telegram_origin()
    srv = _start(tmp_path, CtxReply(owner), mirror_name=configured)
    try:
        _post(srv)
        assert owner.adapter.sent[0][1].startswith(header)
        assert "aaron\\_handle" not in owner.adapter.sent[0][1] and "Beispiel" not in owner.adapter.sent[0][1]
    finally:
        srv.server.stop()
        owner.close()


def _slow_adapter(owner, release):
    original = owner.adapter.send

    async def slow(chat_id, content, reply_to=None, metadata=None):
        await asyncio.get_running_loop().run_in_executor(None, release.wait, 3)
        return await original(chat_id, content, reply_to, metadata)

    owner.adapter.send = slow


@pytest.mark.parametrize("accept", [False, True], ids=["refused", "accepted"])
def test_a_late_mirror_is_deleted_when_the_message_was_refused_meanwhile(tmp_path, monkeypatch, accept):
    from plugins.platforms.werkbank import host as host_mod

    monkeypatch.setattr(host_mod, "MIRROR_WAIT_S", 0.2)
    owner = FakeOwner()
    release = threading.Event()
    _slow_adapter(owner, release)
    ctx = CtxReply(owner, accept=accept)
    srv = _start(tmp_path, ctx)
    try:
        assert _post(srv)[0] == (200 if accept else 503)
        assert ctx.injected[0]["reply_to_message_id"] is None      # too late to be the anchor
        assert owner.adapter.deleted == []                          # not posted yet: nothing to delete
        release.set()                                               # now the mirror reaches the chat
        deadline = time.monotonic() + 3
        while not owner.adapter.sent and time.monotonic() < deadline:
            time.sleep(0.02)
        if accept:
            time.sleep(0.2)
            assert owner.adapter.deleted == []
        else:
            while not owner.adapter.deleted and time.monotonic() < deadline:
                time.sleep(0.02)
            assert owner.adapter.deleted == [("4711", "900")]
    finally:
        release.set()
        srv.server.stop()
        owner.close()


@pytest.mark.parametrize("state", ["not-running", "draining", "injection-not-allowed", "no-gateway"])
def test_no_mirror_when_the_gateway_cannot_take_the_message(tmp_path, state):
    owner = FakeOwner()
    ctx = CtxReply(owner, accept=False)
    if state == "not-running":
        owner._running = False
    elif state == "draining":
        owner._draining = True
    elif state == "injection-not-allowed":
        ctx._gateway_injection_allowed = lambda: False
    srv = _start(tmp_path, ctx)
    try:
        call(srv.port, "GET", f"/v1/resolve?session_id={SID}", srv.key)
        if state == "no-gateway":
            ctx._manager._gateway_message_injector = None
        status, _ = call(srv.port, "POST", session_path("messages"), srv.key,
                         {"text": "Deploy jetzt?", "client_message_id": "m1"})
        assert status == 503
        time.sleep(0.1)
        assert owner.adapter.sent == [] and owner.adapter.deleted == []
    finally:
        srv.server.stop()
        owner.close()


def test_mirror_is_sent_when_injection_is_allowed(tmp_path):
    owner = FakeOwner()
    ctx = CtxReply(owner)
    ctx._gateway_injection_allowed = lambda: True
    srv = _start(tmp_path, ctx)
    try:
        assert _post(srv) == (200, {"accepted": True})
        assert len(owner.adapter.sent) == 1 and ctx.injected[0]["reply_to_message_id"] == "900"
    finally:
        srv.server.stop()
        owner.close()


def test_a_non_numeric_message_id_is_no_reply_anchor(tmp_path):
    """The core takes digit strings only (WB-1148): a Slack-style ts must not get the message refused."""
    owner = FakeOwner()
    owner.adapter.answer = SimpleNamespace(success=True, message_id="1700000000.123")
    ctx = CtxReply(owner)
    srv = _start(tmp_path, ctx)
    try:
        assert _post(srv) == (200, {"accepted": True})
        assert ctx.injected[0]["reply_to_message_id"] is None
    finally:
        srv.server.stop()
        owner.close()


def test_a_non_telegram_chat_gets_the_plain_block_without_the_flag(tmp_path):
    owner = FakeOwner()
    owner.session_store.entry.origin = SessionSource(platform=Platform.DISCORD, chat_id="4711", chat_type="dm",
                                                     user_id="4711", thread_id="51")
    srv = _start(tmp_path, CtxReply(owner))
    try:
        _post(srv)
        assert owner.adapter.sent == [("4711", "👤 Du · Werkbank\n```\nDeploy jetzt?\n```", {"thread_id": "51"})]
    finally:
        srv.server.stop()
        owner.close()


def test_mirror_switch_defaults_to_on_and_port_to_8767():
    from plugins.platforms.werkbank import config

    assert config.from_entry({"enabled": True}) == config.Settings(enabled=True, port=8767,
                                                                    mirror_to_telegram=True, mirror_name="")
    assert config.from_entry({"mirror_to_telegram": False}).mirror_to_telegram is False
    assert config.from_entry({"mirror_name": " Aaron "}).mirror_name == "Aaron"
    assert config.from_entry({"mirror_name": 7}).mirror_name == ""
    assert config.from_entry(None).enabled is False
    assert config.from_entry({"port": 8765}).problem
    assert config.from_entry({"port": "abc"}).problem


# ── snapshot ids == stream ids (WB-1132 review) ────────────────────────────────────────────


@pytest.mark.parametrize("restart", [False, True])
def test_snapshot_and_stream_name_a_message_by_the_same_board_key(tmp_path, monkeypatch, restart):
    from hermes_state import SessionDB

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id=SID, source="telegram")
    owner = FakeOwner(db)
    ctx = Ctx127(owner)
    srv = _start(tmp_path, ctx)
    try:
        _subscribe(srv.buffers)
        call(srv.port, "GET", f"/v1/resolve?session_id={SID}", srv.key)
        epoch = srv.buffers.get(KEY_SESSION).epoch
        text = "Bitte die Tests laufen lassen"
        assert call(srv.port, "POST", session_path("messages"), srv.key,
                    {"text": text, "client_message_id": "wb-42"})[0] == 200
        # What the gateway does with it: announce it, persist the user row with the id the turn
        # stores on it (gateway.run_plugin_injection.client_message_metadata), answer.
        from gateway.run_plugin_injection import client_message_metadata
        injected = _injected_event(ctx.injected[-1])
        session_events.user_message(KEY_SESSION, injected, "turn")
        db.append_message(SID, "user", text, display_metadata=client_message_metadata(injected))
        telegram = MessageEvent(text="und aus Telegram", message_id="tg-7", source=_source(), metadata={})
        session_events.user_message(KEY_SESSION, telegram, "turn", session_id=SID)
        db.append_message(SID, "user", "und aus Telegram", platform_message_id="tg-7")
        db.append_message(SID, "assistant", "Mache ich.")
        _settle(lambda: srv.buffers.get(KEY_SESSION).seq >= 2)

        if restart:                                  # the id is in the row: survives a plugin restart
            srv.server.stop()
            srv = _start(tmp_path, ctx)
            call(srv.port, "GET", f"/v1/resolve?session_id={SID}", srv.key)
        status, snap = call(srv.port, "GET", session_path("snapshot"), srv.key)
        assert status == 200
        users = [m for m in snap["messages"] if m["role"] == "user"]
        assert [(m["id"], m["client_message_id"], m["message_id"]) for m in users] == [
            ("wb-42", "wb-42", None), ("tg-7", None, "tg-7")]
        assert all(isinstance(m["db_id"], int) for m in snap["messages"])
        answer = [m for m in snap["messages"] if m["role"] == "assistant"][0]
        assert answer["id"] == f"db:{answer['db_id']}"
        assert users[0]["origin"] == "werkbank"
        assert not (tmp_path / "plugin-data" / "werkbank" / "messages.json").exists()   # no text map

        if not restart:
            status, events = sse(srv.port, srv.key, events_path(epoch=epoch), 2)
            # hermes_view.js hvStreamUserId: client_message_id || id || message_id
            stream_ids = [str(e["payload"].get("client_message_id") or e["id"] or e["payload"].get("message_id"))
                          for e in events]
            snapshot_ids = [m["client_message_id"] or m["id"] for m in users]   # hvSnapshotUserId
            assert stream_ids == snapshot_ids == ["wb-42", "tg-7"]
    finally:
        srv.server.stop()
        owner.close()


def test_snapshot_reads_history_approvals_and_clarify_through_the_hermes_api(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    import tools.approval as approval
    from tools import clarify_gateway

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id=SID, source="telegram")
    db.append_message(SID, "user", "lies die config")
    db.append_message(SID, "assistant", None, tool_calls=[{"id": "call_1", "type": "function",
                                                            "function": {"name": "terminal", "arguments": "{}"}}])
    db.append_message(SID, "tool", "x" * 9000, tool_call_id="call_1", tool_name="terminal")
    monkeypatch.setattr(approval, "list_gateway_approvals", lambda key: [{
        "request_id": "req-1", "command": "export OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456",
        "description": "dangerous", "pattern_key": "export", "allow_permanent": False}])
    entry = clarify_gateway.register("clar-1", KEY_SESSION, "Welche Variante?", ["A", "B"])
    owner = FakeOwner(db)
    srv = _start(tmp_path, Ctx127(owner))
    try:
        call(srv.port, "GET", f"/v1/resolve?session_id={SID}", srv.key)
        status, snap = call(srv.port, "GET", session_path("snapshot"), srv.key)
        assert status == 200 and snap["state"] == "wartet"
        tool = [m for m in snap["messages"] if m["role"] == "tool"][0]
        assert tool["truncated"] is True and len(tool["text"].encode()) <= 4096
        assert tool["tool_call_id"] == "call_1" and tool["tool_name"] == "terminal"
        assistant = [m for m in snap["messages"] if m["role"] == "assistant"][0]
        assert assistant["tool_calls"] == [{"id": "call_1", "name": "terminal"}]
        approval_ = snap["pending_approvals"][0]
        assert approval_["request_id"] == "req-1" and "sk-abcdefghijklmnop" not in approval_["command"]
        assert approval_["allow_permanent"] is False and approval_["allow_session"] is True
        assert snap["pending_clarify"] == [{"clarify_id": "clar-1", "question": "Welche Variante?",
                                            "choices": ["A", "B"], "multi_select": False,
                                            "awaiting_text": False}]
        full = call(srv.port, "GET", session_path("tools", "call_1", "output"), srv.key)
        assert full[0] == 200 and len(full[1]["output"]) == 9000
    finally:
        clarify_gateway.resolve_gateway_clarify(entry.clarify_id, "A")
        srv.server.stop()
        owner.close()


def test_running_turn_shows_as_arbeitet(tmp_path):
    owner = FakeOwner()
    srv = _start(tmp_path, Ctx127(owner))
    try:
        call(srv.port, "GET", f"/v1/resolve?session_id={SID}", srv.key)
        owner._running_agents[KEY_SESSION] = object()
        assert call(srv.port, "GET", session_path("snapshot"), srv.key)[1]["state"] == "arbeitet"
    finally:
        srv.server.stop()
        owner.close()


def test_resolve_refuses_local_and_foreign_profile_sessions(tmp_path):
    owner = FakeOwner()
    srv = _start(tmp_path, Ctx127(owner))
    try:
        owner.session_store.entry.origin = SessionSource(platform=Platform.LOCAL, chat_id="x")
        assert call(srv.port, "GET", f"/v1/resolve?session_id={SID}", srv.key)[0] == 404
        source = _source()
        source.profile = "other"
        owner.session_store.entry.origin = source
        if source.to_dict().get("profile") == "other":
            assert call(srv.port, "GET", f"/v1/resolve?session_id={SID}", srv.key)[0] == 404
    finally:
        srv.server.stop()
        owner.close()


# ── register(): gateway process only, opt-in ────────────────────────────────────────────────


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_register_does_nothing_outside_the_gateway_or_when_not_enabled(tmp_path, monkeypatch):
    import plugins.platforms.werkbank as plugin

    owner = FakeOwner()
    try:
        ctx = Ctx127(owner)
        ctx._manager.home_path = tmp_path
        monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
        plugin.register(ctx)
        assert ctx.hooks == {} and not (tmp_path / "plugin-data").exists()
        monkeypatch.setenv("_HERMES_GATEWAY", "1")
        monkeypatch.setitem(sys.modules, "gateway.run", SimpleNamespace(gateway_process_started=lambda: False))
        plugin.register(ctx)
        assert ctx.hooks == {}       # gateway.run imported by a CLI, start_gateway() never ran
        monkeypatch.setitem(sys.modules, "gateway.run", SimpleNamespace(gateway_process_started=lambda: True))
        (tmp_path / "config.yaml").write_text("plugins:\n  entries:\n    werkbank:\n      port: 8767\n")
        plugin.register(ctx)
        assert ctx.hooks == {}

        port = _free_port()
        (tmp_path / "config.yaml").write_text(
            f"plugins:\n  entries:\n    werkbank:\n      enabled: true\n      port: {port}\n")
        plugin.register(ctx)
        assert list(ctx.hooks) == [HOOK] and len(ctx.unload) == 1
        key = (tmp_path / "plugin-data" / "werkbank" / "key").read_text().strip()
        assert call(port, "GET", "/v1/resolve?session_id=x_1", key)[0] in (404, 503)
        assert call(port, "GET", "/v1/resolve?session_id=x_1", "wrong")[0] == 401
        ctx.unload[0]()
    finally:
        owner.close()


def test_plugin_is_discovered_as_a_bundled_platform_without_adapter():
    from hermes_cli.plugins_discovery import collect_directory_manifests

    found = [m for m in collect_directory_manifests() if m.key == PLUGIN_ID]
    assert len(found) == 1 and found[0].kind == "platform" and found[0].source == "bundled"
    import plugins.platforms.werkbank as plugin
    assert "register_platform" not in inspect.getsource(plugin)


def test_core_gate_key_is_the_plugin_id_platforms_werkbank(tmp_path):
    """README: allow_gateway_injection lives under ``plugins.entries."platforms/werkbank"``."""
    from hermes_cli.plugins_discovery import collect_directory_manifests
    from hermes_cli.plugins_manifest import manifest_key
    from hermes_cli.plugins_state import _plugin_settings_entry
    from utils import fast_safe_load

    manifest = next(m for m in collect_directory_manifests() if m.name == "werkbank")
    cfg = fast_safe_load("plugins:\n  entries:\n    werkbank:\n      port: 8767\n"
                         "    platforms/werkbank:\n      allow_gateway_injection: true\n")
    assert manifest_key(manifest) == PLUGIN_ID
    assert _plugin_settings_entry(cfg, manifest_key(manifest))["allow_gateway_injection"] is True


def test_host_row_marks_and_lineage_come_from_the_real_sessiondb(tmp_path):
    """Row ids only grow (AUTOINCREMENT); the lineage follows compression, never ``/new``."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id=SID, source="telegram")
    owner = FakeOwner(db)
    try:
        from plugins.platforms.werkbank.host import GatewayHost

        host = GatewayHost(Ctx127(owner), tmp_path)
        assert host.row_watermark(KEY_SESSION, SID) == 0
        first = db.append_message(SID, "user", "a")
        db.append_message(SID, "assistant", "b")
        assert host.row_watermark(KEY_SESSION, SID) > first
        child, fresh = "20261013_130000_cccccc", "20261013_140000_ffffff"
        db.end_session(SID, "compression")
        db.create_session(session_id=child, source="telegram", parent_session_id=SID)
        db.create_session(session_id=fresh, source="telegram")
        assert host.lineage(KEY_SESSION, child) == [SID, child]
        assert host.lineage(KEY_SESSION, fresh) == [fresh]
    finally:
        owner.close()

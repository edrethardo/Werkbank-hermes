"""WB-1342: the board opens a NEW chat for a new project („+“ in the chat list).

``POST /v1/sessions {next_to, title, text}`` — the plugin opens a sibling thread next to the chat
of the session ``next_to`` (a Telegram DM topic, like ``/branch`` does), creates that thread's
session, and starts its first turn with *text* (the board's fixed start instruction) through
``inject_message(origin=…)`` WITHOUT a client message id, so the instruction is no owner bubble.
Answer: ``{session_key, session_id, thread_id}``. A chat that cannot host a thread: 501; the
gateway not taking messages: 503; an unknown ``next_to``: 404 — never a guess, never a second
thread on a retry the board did not ask for.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from werkbank_testkit import KEY_SESSION, SID, FakeHost, call, running_server

from gateway.config import Platform
from gateway.session import SessionSource

NEW_SID = "20261028_090000_feed01"


# -- the HTTP endpoint (server.py) ------------------------------------------------------------------

class _NewHost(FakeHost):
    def __init__(self):
        super().__init__()
        self.created: list = []
        self.answer = {"session_key": "agent:main:telegram:dm:4711:77", "session_id": NEW_SID,
                       "thread_id": "77"}

    def new_session(self, next_to, title, text):
        self.created.append((next_to, title, text))
        if isinstance(self.answer, Exception):
            raise self.answer
        return dict(self.answer)


@pytest.fixture
def srv(tmp_path):
    s = running_server(tmp_path, _NewHost())
    yield s
    s.server.stop()


def post(srv, body, key=True):
    return call(srv.port, "POST", "/v1/sessions", srv.key if key else None, body)


def test_new_session_answers_key_id_and_thread(srv):
    status, body = post(srv, {"next_to": KEY_SESSION, "title": "Neues Projekt", "text": "Start"})
    assert status == 200, body
    assert body == {"session_key": "agent:main:telegram:dm:4711:77", "session_id": NEW_SID,
                    "thread_id": "77"}
    assert srv.host.created == [(KEY_SESSION, "Neues Projekt", "Start")]


def test_new_session_needs_the_key(srv):
    status, _ = post(srv, {"next_to": KEY_SESSION, "title": "Neues Projekt", "text": "Start"}, key=False)
    assert status == 401
    assert srv.host.created == []


@pytest.mark.parametrize("body", [
    {"title": "Neues Projekt", "text": "Start"},
    {"next_to": KEY_SESSION, "title": "", "text": "Start"},
    {"next_to": KEY_SESSION, "title": "x" * 61, "text": "Start"},
    {"next_to": KEY_SESSION, "title": "zwei\nZeilen", "text": "Start"},
    {"next_to": KEY_SESSION, "title": "Neues Projekt", "text": ""},
    {"next_to": KEY_SESSION, "title": "Neues Projekt", "text": 5},
])
def test_new_session_checks_its_input(srv, body):
    status, answer = post(srv, body)
    assert status == 400, answer
    assert srv.host.created == []


def test_new_session_refusal_keeps_status_and_reason(srv):
    from plugins.platforms.werkbank.host import NewSessionRefused

    srv.host.answer = NewSessionRefused(501, "Dieser Chat kann kein neues Thema anlegen.")
    status, body = post(srv, {"next_to": KEY_SESSION, "title": "Neues Projekt", "text": "Start"})
    assert status == 501 and "neues Thema" in body["grund"]


# -- the gateway side (host.py) ---------------------------------------------------------------------

class _Adapter:
    def __init__(self, thread_id="77"):
        self.thread_id = thread_id
        self.threads: list = []

    async def create_handoff_thread(self, parent_chat_id, name):
        self.threads.append((parent_chat_id, name))
        return self.thread_id


class _Store:
    def __init__(self):
        self.created: list = []

    async def get_or_create_session(self, source):
        self.created.append(source)
        return SimpleNamespace(session_key=f"agent:main:telegram:dm:{source.chat_id}:{source.thread_id}",
                               session_id=NEW_SID)


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(2)


def _host(tmp_path, loop, *, adapter=None, inject=True, running=True, platform=Platform.TELEGRAM):
    from plugins.platforms.werkbank.host import GatewayHost

    source = SessionSource(platform=platform, chat_id="4711", chat_type="dm", user_id="4711",
                           thread_id="51")
    adapter = adapter or _Adapter()
    store = _Store()
    injected: list = []
    owner = SimpleNamespace(
        _gateway_loop=loop, _running=running, _draining=False,
        _restored_source=lambda entry: entry.origin, _delivery_adapter_for=lambda src: adapter,
        session_store=SimpleNamespace(lookup_by_session_key=lambda key: (
            SimpleNamespace(origin=source) if key == KEY_SESSION else None)),
        async_session_store=store)

    def inject_message(content, role="user", **kw):
        injected.append((content, kw))
        return inject

    ctx = SimpleNamespace(_manager=SimpleNamespace(_gateway_message_injector=(owner, None)),
                          inject_message=inject_message, _gateway_injection_allowed=lambda: True)
    return GatewayHost(ctx, tmp_path), adapter, store, injected


def test_host_opens_a_sibling_thread_creates_its_session_and_starts_the_turn(tmp_path, loop):
    host, adapter, store, injected = _host(tmp_path, loop)
    out = host.new_session(KEY_SESSION, "Neues Projekt", "Startanweisung")
    assert out == {"session_key": "agent:main:telegram:dm:4711:77", "session_id": NEW_SID,
                   "thread_id": "77"}
    assert adapter.threads == [("4711", "Neues Projekt")]
    assert [(s.chat_id, s.thread_id, s.chat_type) for s in store.created] == [("4711", "77", "dm")]
    assert len(injected) == 1
    text, kw = injected[0]
    assert text == "Startanweisung"
    assert kw["origin"]["thread_id"] == "77" and str(kw["origin"]["chat_id"]) == "4711"
    assert "session_key" not in kw
    # the start instruction is no owner message: no client id, so the board shows no bubble
    assert "client_message_id" not in kw


def test_host_unknown_template_is_404(tmp_path, loop):
    from plugins.platforms.werkbank.host import NewSessionRefused

    host, adapter, _store, injected = _host(tmp_path, loop)
    with pytest.raises(NewSessionRefused) as e:
        host.new_session("agent:main:telegram:dm:4711:999", "Neues Projekt", "x")
    assert e.value.status == 404
    assert adapter.threads == [] and injected == []


def test_host_platform_without_threads_is_501(tmp_path, loop):
    from plugins.platforms.werkbank.host import NewSessionRefused

    host, adapter, _store, injected = _host(tmp_path, loop, platform=Platform.WHATSAPP)
    with pytest.raises(NewSessionRefused) as e:
        host.new_session(KEY_SESSION, "Neues Projekt", "x")
    assert e.value.status == 501
    assert adapter.threads == [] and injected == []


def test_host_thread_not_created_is_502_and_nothing_injected(tmp_path, loop):
    from plugins.platforms.werkbank.host import NewSessionRefused

    host, _adapter, store, injected = _host(tmp_path, loop, adapter=_Adapter(thread_id=None))
    with pytest.raises(NewSessionRefused) as e:
        host.new_session(KEY_SESSION, "Neues Projekt", "x")
    assert e.value.status == 502
    assert store.created == [] and injected == []


def test_host_gateway_not_running_is_503(tmp_path, loop):
    from plugins.platforms.werkbank.host import NewSessionRefused

    host, adapter, _store, injected = _host(tmp_path, loop, running=False)
    with pytest.raises(NewSessionRefused) as e:
        host.new_session(KEY_SESSION, "Neues Projekt", "x")
    assert e.value.status == 503
    assert adapter.threads == [] and injected == []


def test_host_start_refused_is_503_with_the_session_named(tmp_path, loop):
    from plugins.platforms.werkbank.host import NewSessionRefused

    host, _adapter, _store, injected = _host(tmp_path, loop, inject=False)
    with pytest.raises(NewSessionRefused) as e:
        host.new_session(KEY_SESSION, "Neues Projekt", "x")
    assert e.value.status == 503 and NEW_SID in e.value.grund
    # the board keeps it as a draft: the refusal carries what exists (WB-1342 Nachbesserung)
    assert e.value.extra == {"angelegt": True, "session_id": NEW_SID,
                             "session_key": "agent:main:telegram:dm:4711:77", "thread_id": "77"}
    assert len(injected) == 1


def test_host_gateway_not_running_names_nothing(tmp_path, loop):
    from plugins.platforms.werkbank.host import NewSessionRefused

    host, _adapter, _store, _injected = _host(tmp_path, loop, running=False)
    with pytest.raises(NewSessionRefused) as e:
        host.new_session(KEY_SESSION, "Neues Projekt", "x")
    assert e.value.extra == {}


def test_new_session_503_with_session_carries_it(srv):
    from plugins.platforms.werkbank.host import NewSessionRefused

    srv.host.answer = NewSessionRefused(
        503, "Thema und Sitzung sind angelegt, aber Hermes hat die Startanweisung nicht angenommen.",
        extra={"angelegt": True, "session_id": NEW_SID, "session_key": "agent:main:telegram:dm:4711:77",
               "thread_id": "77"})
    status, body = post(srv, {"next_to": KEY_SESSION, "title": "Neues Projekt", "text": "Start"})
    assert status == 503
    assert (body["angelegt"], body["session_id"], body["session_key"], body["thread_id"]) == (
        True, NEW_SID, "agent:main:telegram:dm:4711:77", "77")
    assert body["error"] == "refused" and "Startanweisung" in body["grund"]

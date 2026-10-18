"""WB-1212: a Werkbank message is redacted in the plugin BEFORE its Telegram mirror goes out and
before it is injected — mirror, inject and the event stream carry only the placeholder.

The secret is FAKE and assembled at runtime."""

from __future__ import annotations

import pytest

from agent import secret_intake as si
from werkbank_testkit import KEY_SESSION, SID, call, session_path

FAKE_GH = "ghp" + "_" + "FAKE0fake1FAKE2fake3FAKE4fake5FAKE6f"


@pytest.fixture(autouse=True)
def _intake_on(monkeypatch):
    si.store().clear()
    monkeypatch.setattr(si, "enabled", lambda: True)
    yield
    si.store().clear()


def _resolve(srv):
    status, answer = call(srv.port, "GET", f"/v1/resolve?session_id={SID}", srv.key)
    assert status == 200, answer


def test_mirror_and_inject_carry_only_the_placeholder(plugin_server):
    srv = plugin_server
    _resolve(srv)
    body = {"text": f"Neues Token {FAKE_GH} bitte hinterlegen", "client_message_id": "wb-s1"}
    assert call(srv.port, "POST", session_path("messages"), srv.key, body)[0] == 200
    want = "Neues Token [GEHEIMNIS:github-token-1] bitte hinterlegen"
    assert [m[1] for m in srv.host.mirrored] == [want]
    assert [i[1] for i in srv.host.injected] == [want]
    # the gateway offers the card for it (same session key) — not yet offered by the plugin
    assert [e.placeholder for e in si.offer_candidates(KEY_SESSION, want)] == ["[GEHEIMNIS:github-token-1]"]


def test_switch_off_leaves_the_message(plugin_server, monkeypatch):
    monkeypatch.setattr(si, "enabled", lambda: False)
    srv = plugin_server
    _resolve(srv)
    body = {"text": f"t {FAKE_GH}", "client_message_id": "wb-s2"}
    assert call(srv.port, "POST", session_path("messages"), srv.key, body)[0] == 200
    assert [i[1] for i in srv.host.injected] == [f"t {FAKE_GH}"]

"""WB-1174: attachments from the Werkbank board — upload, bind to a message, hand to the turn.

The plugin server runs on 127.0.0.1 with a fake gateway host (``werkbank_testkit.FakeHost``); files
land under ``tmp_path/cache`` (the profile's media cache in production), never under ``~/.hermes``.
"""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path

import pytest

from werkbank_testkit import FakeHost, KEY_SESSION, SID, call, events_path, running_server, session_path, sse, core_event

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"0" * 64
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 32


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@pytest.fixture
def srv(tmp_path):
    host = FakeHost()
    s = running_server(tmp_path, host)
    call(s.port, "GET", f"/v1/resolve?session_id={SID}", s.key)
    yield s
    s.server.stop()


def _upload(s, data, name="bild.png"):
    return call(s.port, "POST", session_path("uploads"), s.key, {"name": name, "data": _b64(data)})


def test_upload_stores_an_image_owner_only_in_the_profile_media_cache(srv, tmp_path):
    status, answer = _upload(srv, PNG, "Screenshot 2026.png")
    assert status == 200, answer
    assert answer["kind"] == "image" and answer["type"] == "image/png" and answer["size"] == len(PNG)
    assert answer["name"] == "Screenshot 2026.png" and answer["upload_id"].startswith("up_")
    files = list((tmp_path / "cache" / "images").iterdir())
    assert len(files) == 1 and files[0].read_bytes() == PNG
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "cache" / "images").stat().st_mode) == 0o700


@pytest.mark.parametrize("data,name,want_kind,want_type,want_name", [
    (JPEG, "foto.png", "image", "image/jpeg", "foto.jpg"),               # the bytes decide, not the name
    (PDF, "bericht.pdf", "document", "application/pdf", "bericht.pdf"),
    (HEIC, "IMG_0001.HEIC", "document", "application/octet-stream", "IMG_0001.heic"),   # never as an image
    (b"# Notiz\nhallo\n", "notiz.md", "document", "text/markdown", "notiz.md"),
])
def test_type_comes_from_the_magic_bytes(srv, data, name, want_kind, want_type, want_name):
    status, answer = _upload(srv, data, name)
    assert status == 200, answer
    assert (answer["kind"], answer["type"], answer["name"]) == (want_kind, want_type, want_name)


@pytest.mark.parametrize("data,name", [
    (b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "x.svg"),
    (b"MZ\x90\x00" + b"\x00" * 60, "programm.exe"),
    (b"#!/bin/sh\nrm -rf /\n", "skript.sh"),
    (b"\x00\x01binary", "daten.txt"),                                    # text with NUL is not text
    (b"PK\x03\x04" + b"\x00" * 30, "archiv.zip"),                         # zip only as an office file
])
def test_unknown_types_are_refused_415(srv, data, name):
    status, answer = _upload(srv, data, name)
    assert status == 415 and answer["error"] == "unsupported type"


@pytest.mark.parametrize("name,want", [
    ("../../etc/passwd.png", "passwd.png"),
    ("..\\..\\boot.ini.png", "boot.ini.png"),
    ("/abs/olut.png", "olut.png"),
    ("\u202egnp.exe", "gnp.png"),                                       # bidi override gone, type decides
    ("a\x00b\nc.png", "abc.png"),
    ("", "anhang.png"),
    ("....png", "png.png"),                                             # dots never lead
    ("...", "anhang.png"),
    ("x" * 300 + ".png", "x" * 76 + ".png"),
])
def test_names_are_cleaned_and_never_leave_the_cache(srv, tmp_path, name, want):
    status, answer = _upload(srv, PNG, name)
    assert status == 200, answer
    assert answer["name"] == want
    for path in (tmp_path / "cache").rglob("*"):
        assert path.resolve().is_relative_to((tmp_path / "cache").resolve())


def test_too_large_is_refused_before_the_body_is_read(srv, monkeypatch):
    from plugins.platforms.werkbank import server, uploads
    monkeypatch.setattr(uploads, "MAX_FILE", 100)
    status, answer = _upload(srv, PNG + b"\x00" * 200)
    assert status == 413
    monkeypatch.setattr(server, "MAX_UPLOAD_BODY", 50)
    status, answer = _upload(srv, PNG)
    assert status == 413 and answer["error"] == "too large"


def test_bad_base64_and_unknown_session(srv):
    assert call(srv.port, "POST", session_path("uploads"), srv.key, {"name": "a.png", "data": "!!!"})[0] == 400
    assert call(srv.port, "POST", session_path("uploads", key="agent:main:telegram:dm:1:2"), srv.key,
                {"name": "a.png", "data": _b64(PNG)})[0] == 404


def test_too_many_open_uploads_429(srv, monkeypatch):
    from plugins.platforms.werkbank import uploads
    monkeypatch.setattr(uploads, "MAX_OPEN", 2)
    assert _upload(srv, PNG)[0] == 200 and _upload(srv, PNG)[0] == 200
    assert _upload(srv, PNG)[0] == 429


def _send(s, text, cmid, attachments=None):
    body = {"text": text, "client_message_id": cmid}
    if attachments is not None:
        body["attachments"] = attachments
    return call(s.port, "POST", session_path("messages"), s.key, body)


def test_message_with_attachments_hands_the_files_to_inject(srv, tmp_path):
    a = _upload(srv, PNG, "bild.png")[1]
    b = _upload(srv, PDF, "bericht.pdf")[1]
    assert _send(srv, "schau mal", "wb-1", [a["upload_id"], b["upload_id"]]) == (200, {"accepted": True})
    media = srv.host.media[-1]
    assert [t for _, t in media] == ["image/png", "application/pdf"]
    assert all(Path(p).is_file() and Path(p).resolve().is_relative_to(tmp_path / "cache") for p, _ in media)
    assert srv.host.injected[-1] == (KEY_SESSION, "schau mal", "wb-1")
    # the mirror lists the files; the image goes out as a photo after the text
    assert "bild.png" in srv.host.mirrored[-1][1] and "bericht.pdf" in srv.host.mirrored[-1][1]
    assert srv.host.photos == [(KEY_SESSION, [media[0][0]], None)]


def test_attachments_without_text_are_a_message(srv):
    a = _upload(srv, PNG)[1]
    assert _send(srv, "", "wb-2", [a["upload_id"]]) == (200, {"accepted": True})
    assert srv.host.injected[-1][1] == ""


def test_text_only_message_is_unchanged(srv):
    assert _send(srv, "nur text", "wb-3") == (200, {"accepted": True})
    assert srv.host.media[-1] is None and srv.host.photos == []
    assert srv.host.mirrored[-1][1] == "nur text"
    assert _send(srv, "nur text", "wb-3")[0] == 409                    # dedupe stays
    assert _send(srv, "   ", "wb-4")[0] == 400                         # empty text without attachments


@pytest.mark.parametrize("ids", [["up_unbekannt"], "up_x", [], [1], ["a"] * 11])
def test_unknown_or_malformed_attachment_ids_are_400(srv, ids):
    status, answer = _send(srv, "x", "wb-5", ids)
    assert status == 400 and answer["accepted"] is False
    assert srv.host.injected == []
    assert _send(srv, "x", "wb-5")[0] == 200                            # the id was released again


def test_an_attachment_binds_to_one_message_only(srv):
    a = _upload(srv, PNG)[1]
    assert _send(srv, "eins", "wb-6", [a["upload_id"]])[0] == 200
    assert _send(srv, "zwei", "wb-7", [a["upload_id"]])[0] == 400


def test_a_refused_message_frees_its_attachments(srv):
    a = _upload(srv, PNG)[1]
    srv.host.inject_answer = False
    assert _send(srv, "eins", "wb-8", [a["upload_id"]])[0] == 503
    srv.host.inject_answer = True
    assert _send(srv, "nochmal", "wb-9", [a["upload_id"]])[0] == 200


def test_core_without_media_support_answers_501_and_keeps_text_working(srv):
    srv.host.supports_media = False
    a = _upload(srv, PNG)[1]
    status, answer = _send(srv, "x", "wb-10", [a["upload_id"]])
    assert status == 501 and "Anhänge" in answer["grund"]
    assert _send(srv, "x", "wb-10") == (200, {"accepted": True})


def test_other_sessions_cannot_use_or_read_an_upload(tmp_path):
    host = FakeHost()
    other_key, other_sid = "agent:main:telegram:dm:4711:52", "20261013_130000_aaaaaa"
    host.sessions[other_sid] = {"session_key": other_key, "session_id": other_sid, "platform": "telegram",
                                "live": True}
    s = running_server(tmp_path, host)
    try:
        call(s.port, "GET", f"/v1/resolve?session_id={SID}", s.key)
        call(s.port, "GET", f"/v1/resolve?session_id={other_sid}", s.key)
        a = _upload(s, PNG)[1]
        assert call(s.port, "GET", session_path("uploads", a["upload_id"], key=other_key), s.key)[0] == 404
        status, _ = call(s.port, "POST", session_path("messages", key=other_key), s.key,
                         {"text": "x", "client_message_id": "wb-11", "attachments": [a["upload_id"]]})
        assert status == 400
    finally:
        s.server.stop()


def test_get_upload_serves_the_file_with_its_type(srv):
    a = _upload(srv, PNG)[1]
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    conn.request("GET", session_path("uploads", a["upload_id"]), headers={"Authorization": "Bearer " + srv.key})
    resp = conn.getresponse()
    assert resp.status == 200 and resp.read() == PNG
    assert resp.getheader("Content-Type") == "image/png"
    assert resp.getheader("X-Content-Type-Options") == "nosniff"
    conn.close()
    assert call(srv.port, "GET", session_path("uploads", "up_fehlt"), srv.key)[0] == 404


def test_user_message_event_and_snapshot_name_the_attachments(srv):
    a = _upload(srv, PNG, "bild.png")[1]
    assert _send(srv, "schau", "wb-12", [a["upload_id"]])[0] == 200
    srv.buffers.on_event(core_event(1, "user.message", {"text": "schau", "client_message_id": "wb-12",
                                                        "delivery": "turn", "media_count": 1},
                                    id_="wb-12", origin="werkbank"))
    epoch = srv.buffers.get(KEY_SESSION).epoch
    status, events = sse(srv.port, srv.key, events_path(epoch=epoch), 1)
    want = [{"id": a["upload_id"], "name": "bild.png", "type": "image/png", "size": len(PNG), "kind": "image"}]
    assert events[0]["payload"]["attachments"] == want
    srv.host.messages.append({"id": 7, "role": "user", "content": "schau", "timestamp": 1.0,
                              "display_metadata": {"client_message_id": "wb-12",
                                                   "hermes_plugin_id": "platforms/werkbank"}})
    status, snap = call(srv.port, "GET", session_path("snapshot"), srv.key)
    row = [m for m in snap["messages"] if m.get("client_message_id") == "wb-12"][0]
    assert row["attachments"] == want


def test_unbound_uploads_expire_and_their_files_go(tmp_path):
    from plugins.platforms.werkbank.uploads import OPEN_TTL_S, Uploads
    now = [1000.0]
    store = Uploads(tmp_path, clock=lambda: now[0])
    rec = store.store(KEY_SESSION, "a.png", PNG)
    path = store.file(KEY_SESSION, rec["id"])[0]
    assert os.path.exists(path)
    now[0] += OPEN_TTL_S + 1
    store.sweep()
    assert store.file(KEY_SESSION, rec["id"]) is None and not os.path.exists(path)

"""WB-1321: files Hermes SENDS (``MEDIA:<path>`` in a reply) reach the Werkbank chat like Telegram's.

The outbox copies each file the gateway's own filter lets through (``outbox.py``), the ``turn.done``
event and the snapshot carry metadata only, the tags leave the text, and ``GET /v1/s/{key}/files/…``
serves the copy of THIS session with single-range support. The playback versions (WebP previews,
poster, faststart MP4, AAC) come from a real ``ffmpeg`` on small media made in the test itself.
Everything lands under ``tmp_path``; nothing under ``~/.hermes``.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import stat
import struct
import subprocess
import threading
from pathlib import Path

import pytest

from werkbank_testkit import FakeHost, KEY_SESSION, SID, call, core_event, events_path, running_server, session_path, sse

from plugins.platforms.werkbank import medien, outbox as outbox_mod

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not installed")

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
PDF = b"%PDF-1.7\n" + b"0" * 300
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x11" * 300
HTML = b"<!doctype html><script>alert(document.cookie)</script>"


def _files(tmp_path, **named):
    out = {}
    for name, data in named.items():
        p = tmp_path / "src" / name.replace("_", ".")
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(data)
        out[name] = p
    return out


def _server(tmp_path, *, ffmpeg=False, **kw):
    werk = medien.Werkstatt(synchron=True) if ffmpeg else medien.Werkstatt(ffmpeg=None, ffprobe=None)
    box = outbox_mod.Outbox(tmp_path / "home", werkstatt=werk, **kw)
    srv = running_server(tmp_path, FakeHost(), outbox=box)
    status, _ = call(srv.port, "GET", f"/v1/resolve?session_id={SID}", srv.key)
    assert status == 200
    return srv


def _done(srv, text, *, uid=None, turn="turn_1", seq=1, key=KEY_SESSION):
    payload = {"final_response": text, "completed": True}
    if uid:
        payload["message_uid"] = uid
    event = core_event(seq, "turn.done", payload, id_=turn, key=key)
    srv.buffers.on_event(event)
    return event


def _stream(srv, count=1):
    epoch = srv.buffers.get(KEY_SESSION).epoch
    status, events = sse(srv.port, srv.key, events_path(epoch=epoch, after=0), count=count, timeout=2.0)
    assert status == 200
    return events


def _get(port, path, key, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path, headers={"Authorization": "Bearer " + key, **(headers or {})})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


# ── the event: metadata, no tags, copies ──────────────────────────────────────────────────────


def test_bild_pdf_audio_html_kommen_als_dateien_ohne_media_text(tmp_path):
    src = _files(tmp_path, bild_png=PNG, bericht_pdf=PDF, ansage_mp3=MP3, seite_html=HTML)
    srv = _server(tmp_path)
    try:
        text = ("Hier sind die Dateien.\n"
                + "\n".join(f"MEDIA:{p}" for p in src.values()))
        event = _done(srv, text)
        [ev] = _stream(srv)
        assert ev["type"] == "turn.done"
        assert "MEDIA:" not in ev["payload"]["final_response"]
        assert ev["payload"]["final_response"] == "Hier sind die Dateien."
        files = ev["payload"]["files"]
        got = {f["name"]: (f["kind"], f["type"], f["nur_download"]) for f in files}
        assert got == {"bild.png": ("bild", "image/png", False),
                       "bericht.pdf": ("datei", "application/pdf", False),
                       "ansage.mp3": ("audio", "audio/mpeg", False),
                       "seite.html": ("datei", "text/html", True)}      # HTML: only ever a download
        assert all(outbox_mod.ID_RE.fullmatch(f["id"]) for f in files)
        assert len({f["id"] for f in files}) == 4
        assert {f["size"] for f in files} == {len(PNG), len(PDF), len(MP3), len(HTML)}
        # the core's own event dict (what Telegram's delivery reads) is untouched
        assert event["payload"]["final_response"] == text and "files" not in event["payload"]
        # copies, owner-only, never the original path
        root = tmp_path / "home" / "plugin-data" / "werkbank" / "outbox"
        copies = sorted(root.glob("*/fx_*/original"))
        assert len(copies) == 4
        for c in copies:
            assert stat.S_IMODE(c.stat().st_mode) == 0o600
            assert stat.S_IMODE(c.parent.stat().st_mode) == 0o700
        src["bild_png"].write_bytes(b"changed")
        fid = next(f["id"] for f in files if f["name"] == "bild.png")
        status, _h, body = _get(srv.port, session_path("files", fid, "original"), srv.key)
        assert status == 200 and body == PNG
    finally:
        srv.server.stop()


def test_ohne_media_bleibt_das_ereignis_unveraendert(tmp_path):
    srv = _server(tmp_path)
    try:
        _done(srv, "nur Text")
        [ev] = _stream(srv)
        assert ev["payload"] == {"final_response": "nur Text", "completed": True}
    finally:
        srv.server.stop()


def test_zu_grosse_datei_wird_karte_ohne_bytes(tmp_path):
    src = _files(tmp_path, gross_pdf=PDF)
    srv = _server(tmp_path, max_file=100)
    try:
        _done(srv, f"MEDIA:{src['gross_pdf']}")
        [ev] = _stream(srv)
        [f] = ev["payload"]["files"]
        assert f["id"] is None and f["fehlt"] == "zu_gross" and f["grenze"] == 100
        assert f["size"] == len(PDF) and f["name"] == "gross.pdf"
        assert not list((tmp_path / "home").rglob("original"))
    finally:
        srv.server.stop()


def test_nicht_beobachtete_sitzung_kopiert_nichts(tmp_path):
    src = _files(tmp_path, bild_png=PNG)
    srv = _server(tmp_path)
    try:
        _done(srv, f"MEDIA:{src['bild_png']}", key="agent:main:telegram:dm:1:2")
        assert not list((tmp_path / "home").rglob("original"))
    finally:
        srv.server.stop()


def test_nur_media_antwort_erscheint_im_snapshot_mit_dateien(tmp_path):
    src = _files(tmp_path, bild_png=PNG)
    srv = _server(tmp_path)
    try:
        srv.buffers.on_event(core_event(1, "turn.start", {}, id_="turn_1"))
        payload = {"final_response": f"MEDIA:{src['bild_png']}", "completed": True}
        srv.buffers.on_event(core_event(2, "turn.done", payload, id_="turn_1"))
        status, snap = call(srv.port, "GET", session_path("snapshot"), srv.key)
        assert status == 200
        [m] = [m for m in snap["messages"] if m["role"] == "assistant"]
        assert m["from_buffer"] and m["text"] == "" and [f["name"] for f in m["files"]] == ["bild.png"]
    finally:
        srv.server.stop()


def test_snapshot_zeile_ohne_tags_mit_dateien_und_gleicher_id(tmp_path):
    src = _files(tmp_path, bild_png=PNG)
    srv = _server(tmp_path)
    try:
        _done(srv, f"Bild:\nMEDIA:{src['bild_png']}", uid="uid-1")
        [ev] = _stream(srv)
        fid = ev["payload"]["files"][0]["id"]
        srv.host.messages.append({"id": 7, "role": "assistant", "message_uid": "uid-1",
                                  "content": f"Bild:\nMEDIA:{src['bild_png']}", "timestamp": 1})
        for _ in range(2):                                   # twice: no second copy, same id
            status, snap = call(srv.port, "GET", session_path("snapshot"), srv.key)
            [m] = [m for m in snap["messages"] if m["role"] == "assistant"]
            assert m["text"] == "Bild:" and [f["id"] for f in m["files"]] == [fid]
        assert len(list((tmp_path / "home").rglob("original"))) == 1
    finally:
        srv.server.stop()


def test_alte_zeile_aus_dem_verlauf_wird_beim_snapshot_uebernommen(tmp_path):
    src = _files(tmp_path, bericht_pdf=PDF)
    srv = _server(tmp_path)
    try:
        srv.host.messages.append({"id": 3, "role": "assistant", "message_uid": "alt",
                                  "content": f"MEDIA:{src['bericht_pdf']}", "timestamp": 1})
        status, snap = call(srv.port, "GET", session_path("snapshot"), srv.key)
        [m] = [m for m in snap["messages"] if m["role"] == "assistant"]
        assert "MEDIA:" not in m["text"] and m["files"][0]["type"] == "application/pdf"
    finally:
        srv.server.stop()


# ── the file route ───────────────────────────────────────────────────────────────────────────


def _one(tmp_path, data=PDF, name="bericht_pdf", **kw):
    src = _files(tmp_path, **{name: data})
    srv = _server(tmp_path, **kw)
    _done(srv, f"MEDIA:{src[name]}")
    [ev] = _stream(srv)
    return srv, ev["payload"]["files"][0]["id"]


def test_range_antworten(tmp_path):
    srv, fid = _one(tmp_path)
    try:
        path = session_path("files", fid, "original")
        status, head, body = _get(srv.port, path, srv.key)
        assert status == 200 and body == PDF and head["Accept-Ranges"] == "bytes"
        assert head["Content-Type"] == "application/pdf" and head["X-Content-Type-Options"] == "nosniff"
        status, head, body = _get(srv.port, path, srv.key, {"Range": "bytes=0-9"})
        assert status == 206 and body == PDF[:10]
        assert head["Content-Range"] == f"bytes 0-9/{len(PDF)}" and head["Content-Length"] == "10"
        status, head, body = _get(srv.port, path, srv.key, {"Range": "bytes=-10"})      # the last bytes
        assert status == 206 and body == PDF[-10:]
        assert head["Content-Range"] == f"bytes {len(PDF) - 10}-{len(PDF) - 1}/{len(PDF)}"
        status, head, body = _get(srv.port, path, srv.key, {"Range": "bytes=100-"})     # resume
        assert status == 206 and body == PDF[100:]
        status, head, body = _get(srv.port, path, srv.key, {"Range": f"bytes={len(PDF)}-"})
        assert status == 416 and head["Content-Range"] == f"bytes */{len(PDF)}" and body == b""
        status, head, body = _get(srv.port, path, srv.key, {"Range": "bytes=5-2"})     # malformed: whole
        assert status == 200 and body == PDF
    finally:
        srv.server.stop()


def test_route_nur_mit_schluessel_und_nur_fuer_die_eigene_sitzung(tmp_path):
    srv, fid = _one(tmp_path)
    try:
        path = session_path("files", fid, "original")
        assert _get(srv.port, path, "falsch")[0] == 401
        status, meta = call(srv.port, "GET", session_path("files", fid), srv.key)
        assert status == 200 and meta["id"] == fid and "src" not in meta and "refs" not in meta
        other = "agent:main:telegram:dm:9:9"
        srv.host.sessions["20261013_120000_zzzzzz"] = {"session_key": other, "session_id": "20261013_120000_zzzzzz",
                                                       "platform": "telegram", "live": True}
        assert call(srv.port, "GET", "/v1/resolve?session_id=20261013_120000_zzzzzz", srv.key)[0] == 200
        assert _get(srv.port, session_path("files", fid, "original", key=other), srv.key)[0] == 404
        assert _get(srv.port, session_path("files", "fx_" + "0" * 32, "original"), srv.key)[0] == 404
        assert _get(srv.port, session_path("files", "..", "original"), srv.key)[0] == 400
        assert _get(srv.port, session_path("files", "../../key"), srv.key)[0] in (400, 404)
        assert _get(srv.port, session_path("files", fid, "meta.json"), srv.key)[0] == 400
        assert _get(srv.port, session_path("files", fid, "mp4"), srv.key)[0] == 404      # no version yet
    finally:
        srv.server.stop()


def test_ohne_ffmpeg_nur_original_mit_grund(tmp_path):
    srv, fid = _one(tmp_path, data=PNG, name="bild_png")
    try:
        status, meta = call(srv.port, "GET", session_path("files", fid), srv.key)
        assert meta["fassungen"] == {} and "ffmpeg fehlt" in meta["grund"]
    finally:
        srv.server.stop()


# ── housekeeping ─────────────────────────────────────────────────────────────────────────────


def test_aufraeumen_nach_alter_und_gesamtgroesse(tmp_path):
    now = [1000.0]
    big = PDF + b"0" * 20_000                       # meta.json (< 1 KB) hardly counts against it
    box = outbox_mod.Outbox(tmp_path / "home", werkstatt=None, clock=lambda: now[0], keep_s=100,
                            max_total=2 * len(big) + 3000)
    src = _files(tmp_path, a_pdf=big, b_pdf=big + b"1", c_pdf=big + b"22")
    ids = []
    for n, name in enumerate(("a_pdf", "b_pdf", "c_pdf")):
        now[0] += 1
        ids.append(box.ingest(KEY_SESSION, [(str(src[name]), False)], [f"r{n}"])[0]["id"])
    assert box.get(KEY_SESSION, ids[0]) is None                      # oldest went: over the total size
    assert box.get(KEY_SESSION, ids[1]) and box.get(KEY_SESSION, ids[2])
    now[0] += 500
    assert box.prune() == 2
    assert not list((tmp_path / "home").rglob("original"))


def test_index_ueberlebt_neustart(tmp_path):
    src = _files(tmp_path, a_pdf=PDF)
    box = outbox_mod.Outbox(tmp_path / "home")
    [f] = box.ingest(KEY_SESSION, [(str(src["a_pdf"]), False)], ["uid-x"])
    again = outbox_mod.Outbox(tmp_path / "home")
    assert [g["id"] for g in again.for_refs(KEY_SESSION, ["uid-x"])] == [f["id"]]
    assert again.get("agent:other", f["id"]) is None


def test_parse_range():
    pr = outbox_mod.parse_range
    assert pr(None, 10) is None and pr("bytes=0-", 10) == (0, 9) and pr("bytes=2-4", 10) == (2, 4)
    assert pr("bytes=-3", 10) == (7, 9) and pr("bytes=-30", 10) == (0, 9) and pr("bytes=8-99", 10) == (8, 9)
    assert pr("bytes=10-", 10) == "416" and pr("bytes=-0", 10) == "416"
    assert pr("bytes=0-1,4-5", 10) is None and pr("items=0-1", 10) is None


# ── the workshop: one job at a time, previews first ─────────────────────────────────────────


def test_werkstatt_ein_job_zur_zeit_vorschau_zuerst():
    w = medien.Werkstatt(ffmpeg=None, ffprobe=None)
    gate, started, order, running, peak = threading.Event(), threading.Event(), [], [0], [0]

    def job(name, wait=False):
        def run():
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            if wait:
                started.set()
                gate.wait(5)
            order.append(name)
            running[0] -= 1
        return run

    w.submit(job("erst", wait=True), prio=1)
    assert started.wait(5)                        # the worker holds the first job: the rest queue up
    w.submit(job("mp4-a"), prio=1)
    w.submit(job("hls-a"), prio=2)
    w.submit(job("vorschau-b"), prio=0)
    gate.set()
    assert w.wait_idle(10)
    assert order == ["erst", "vorschau-b", "mp4-a", "hls-a"] and peak[0] == 1
    assert threading.current_thread().name != "werkbank-medien"
    w.stop()


def test_werkstatt_laeuft_nice_und_ionice():
    w = medien.Werkstatt(ffmpeg="/bin/true", ffprobe="/bin/true")
    if shutil.which("nice"):
        assert w._prefix[:3] == [shutil.which("nice"), "-n", "15"]
    if shutil.which("ionice"):
        assert [shutil.which("ionice"), "-c", "3"] == w._prefix[-3:]


# ── real ffmpeg ──────────────────────────────────────────────────────────────────────────────


def _make(tmp_path, name, *args):
    out = tmp_path / "src" / name
    out.parent.mkdir(exist_ok=True)
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", *args, str(out)], check=True, timeout=120)
    return out


def _probe(path):
    done = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                           str(path)], capture_output=True, check=True, timeout=60)
    return json.loads(done.stdout)


def top_atoms(path):
    names, data = [], Path(path).read_bytes()
    at = 0
    while at + 8 <= len(data):
        size, name = struct.unpack(">I4s", data[at:at + 8])
        if size == 1:
            size = struct.unpack(">Q", data[at + 8:at + 16])[0]
        names.append(name.decode("latin-1"))
        if size < 8:
            break
        at += size
    return names


def _faststart(path):
    atoms = top_atoms(path)
    return "moov" in atoms and "mdat" in atoms and atoms.index("moov") < atoms.index("mdat")


def _ingest(tmp_path, path, voice=False):
    box = outbox_mod.Outbox(tmp_path / "home", werkstatt=medien.Werkstatt(synchron=True))
    [f] = box.ingest(KEY_SESSION, [(str(path), voice)], ["r"])
    meta = box.get(KEY_SESSION, f["id"])
    return box, meta


@needs_ffmpeg
def test_video_wird_faststart_h264_aac_mit_poster(tmp_path):
    src = _make(tmp_path, "roh.avi", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=2",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "mpeg4", "-c:a", "pcm_s16le")
    box, meta = _ingest(tmp_path, src)
    assert meta["kind"] == "video" and meta["fassungen"] == {"poster": "bereit", "mp4": "bereit"}
    assert meta["mp4_weg"] == "kodiert" and meta["abspielbar"] is False and 1.5 <= meta["dauer"] <= 2.5
    mp4, ctype = box.path(KEY_SESSION, meta["id"], "mp4")
    assert ctype == "video/mp4" and _faststart(mp4)
    info = _probe(mp4)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert (v["codec_name"], v["profile"], v["pix_fmt"]) == ("h264", "High", "yuv420p") and v["level"] <= 41
    assert a["codec_name"] == "aac" and 100_000 <= int(a["bit_rate"]) <= 140_000
    poster, ptype = box.path(KEY_SESSION, meta["id"], "poster")
    assert ptype == "image/jpeg" and poster.read_bytes()[:3] == b"\xff\xd8\xff" and poster.stat().st_size < 40_000
    for f in (mp4, poster):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert not list(mp4.parent.glob("*.part"))


@needs_ffmpeg
def test_h264_aac_ohne_faststart_wird_nur_remuxt(tmp_path):
    src = _make(tmp_path, "fertig.mp4", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=2",
                "-f", "lavfi", "-i", "sine=duration=2", "-c:v", "libx264", "-profile:v", "main",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest")
    assert not _faststart(src)                     # ffmpeg's default: the index at the end
    box, meta = _ingest(tmp_path, src)
    assert meta["mp4_weg"] == "remux" and meta["abspielbar"] is True
    mp4, _ = box.path(KEY_SESSION, meta["id"], "mp4")
    assert _faststart(mp4)
    v = next(s for s in _probe(mp4)["streams"] if s["codec_type"] == "video")
    assert v["profile"] == "Main"                  # copied, not re-encoded


@needs_ffmpeg
def test_video_hoechstens_1080p_seitenverhaeltnis_bleibt(tmp_path):
    src = _make(tmp_path, "hoch.mkv", "-f", "lavfi", "-i", "testsrc=size=1440x2560:rate=5:duration=1",
                "-c:v", "mpeg4", "-q:v", "10")
    box, meta = _ingest(tmp_path, src)
    mp4, _ = box.path(KEY_SESSION, meta["id"], "mp4")
    v = next(s for s in _probe(mp4)["streams"] if s["codec_type"] == "video")
    assert v["width"] == 1080 and abs(v["height"] - 1920) <= 2 and v["height"] % 2 == 0


@needs_ffmpeg
def test_ogg_opus_wird_aac_m4a(tmp_path):
    src = _make(tmp_path, "sprache.ogg", "-f", "lavfi", "-i", "sine=duration=1", "-c:a", "libopus")
    box, meta = _ingest(tmp_path, src, voice=True)
    assert meta["kind"] == "audio" and meta["voice"] is True and meta["fassungen"] == {"m4a": "bereit"}
    m4a, ctype = box.path(KEY_SESSION, meta["id"], "m4a")
    assert ctype == "audio/mp4" and _faststart(m4a)
    a = next(s for s in _probe(m4a)["streams"] if s["codec_type"] == "audio")
    assert a["codec_name"] == "aac"


@needs_ffmpeg
def test_bild_bekommt_kleine_und_grosse_webp_vorschau(tmp_path):
    src = _make(tmp_path, "foto.png", "-f", "lavfi", "-i", "testsrc=size=3000x2000:duration=1", "-frames:v", "1")
    box, meta = _ingest(tmp_path, src)
    assert meta["fassungen"] == {"klein": "bereit", "gross": "bereit"}
    klein, ktype = box.path(KEY_SESSION, meta["id"], "klein")
    gross, _ = box.path(KEY_SESSION, meta["id"], "gross")
    assert ktype == "image/webp" and klein.read_bytes()[8:12] == b"WEBP"
    k = next(s for s in _probe(klein)["streams"])
    g = next(s for s in _probe(gross)["streams"])
    assert (k["width"], k["height"]) == (360, 240) and g["width"] == 2048 and abs(g["height"] - 1365) <= 1
    assert klein.stat().st_size < 40_000 and gross.stat().st_size < src.stat().st_size

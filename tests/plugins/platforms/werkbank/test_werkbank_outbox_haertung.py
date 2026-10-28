"""WB-1321 Härtung: the outbox's ffmpeg never probes freely, opens no FIFO or symlinked dir, stays
inside size/time/disk limits, does not redo failed takeovers on every snapshot, and file
transfers have their own quota.

Everything lands under ``tmp_path``; nothing under ``~/.hermes``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from werkbank_testkit import KEY_SESSION, call, session_path

from plugins.platforms.werkbank import medien, outbox as outbox_mod

from test_werkbank_outbox import PDF, PNG, _files, _get, _one, _server  # noqa: F401  (fixtures reused)

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not installed")

SDP = (b"v=0\no=- 0 0 IN IP4 127.0.0.1\ns=x\nc=IN IP4 127.0.0.1\nt=0 0\n"
       b"m=video 59990 RTP/AVP 96\na=rtpmap:96 H264/90000\n")


class Recorder:
    """Stands in for ``subprocess.run`` in ``medien``: records each command, writes the output
    file (the last argument) when *write* is set and answers with *rc*."""

    def __init__(self, rc=0, write=True):
        self.calls, self.rc, self.write = [], rc, write

    def __call__(self, cmd, **_kw):
        self.calls.append(list(cmd))
        if self.write and self.rc == 0 and "ffprobe" not in os.path.basename(cmd[0]):
            Path(cmd[-1]).write_bytes(b"x" * 10)
        return subprocess.CompletedProcess(cmd, self.rc, stdout=b"{}", stderr=b"")


def _fake_box(tmp_path, monkeypatch, rec, **kw):
    monkeypatch.setattr(medien.subprocess, "run", rec)
    werk = medien.Werkstatt(ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe", synchron=True)
    return outbox_mod.Outbox(tmp_path / "home", werkstatt=werk, **kw)


# ── (1) ffmpeg/ffprobe: only the file protocol, the demuxer the sniff found ──────────────────

FORMATE = {
    "bild_jpg": (b"\xff\xd8\xff\xe0" + b"\x00" * 100, "jpeg_pipe"),
    "bild_png": (PNG, "png_pipe"),
    "bild_gif": (b"GIF89a" + b"\x00" * 100, "gif"),
    "bild_webp": (b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 100, "webp_pipe"),
    "film_mp4": (b"\x00\x00\x00\x18ftypisom" + b"\x00" * 100, "mov"),
    "film_mov": (b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 100, "mov"),
    "film_mkv": (b"\x1a\x45\xdf\xa3" + b"\x00" * 100, "matroska"),
    "film_webm": (b"\x1a\x45\xdf\xa3" + b"\x01" * 100, "matroska"),
    "film_avi": (b"RIFF\x00\x00\x00\x00AVI " + b"\x00" * 100, "avi"),
    "ton_ogg": (b"OggS" + b"\x00" * 100, "ogg"),
}


def _inputs(cmd):
    """``[(options before -i since the previous -i/start, input path)]`` of one command."""
    out, start = [], 0
    for i, arg in enumerate(cmd):
        if arg == "-i":
            out.append((cmd[start:i], cmd[i + 1]))
            start = i + 2
    return out


def test_jedes_ffmpeg_und_ffprobe_nur_datei_protokoll_und_erzwungenes_format(tmp_path, monkeypatch):
    rec = Recorder()
    box = _fake_box(tmp_path, monkeypatch, rec)
    src = _files(tmp_path, **{k: v[0] for k, v in FORMATE.items()})
    seen = {}
    for name, (_data, demux) in FORMATE.items():
        before = len(rec.calls)
        [f] = box.ingest(KEY_SESSION, [(str(src[name]), False)], [name])
        assert f["id"], name
        cmds = rec.calls[before:]
        assert cmds, name                               # every media kind ran at least one tool
        for cmd in cmds:
            ins = _inputs(cmd)
            assert ins, cmd
            for opts, _path in ins:
                assert opts[-4:] == ["-protocol_whitelist", "file", "-f", demux], (name, cmd)
        seen[name] = len(cmds)
    assert any("ffprobe" in os.path.basename(c[0]) for c in rec.calls)     # the probe is covered too


def test_werkstatt_verweigert_eingang_ohne_whitelist_und_format(tmp_path, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(medien.subprocess, "run", rec)
    w = medien.Werkstatt(ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe")
    src = str(tmp_path / "x")
    assert w.ffmpeg(["-i", src, "-frames:v", "1"], tmp_path / "o.webp", fmt="webp") is False
    assert w.ffmpeg(["-protocol_whitelist", "file", "-i", src], tmp_path / "o.webp", fmt="webp") is False
    assert w.ffmpeg_raw(["-f", "mov", "-i", src, str(tmp_path / "o.m3u8")]) is False
    assert w.probe(Path(src)) is None                                     # no demuxer named: no probe
    assert rec.calls == []
    assert w.ffmpeg(["-protocol_whitelist", "file", "-f", "png_pipe", "-i", src], tmp_path / "o.webp",
                    fmt="webp") is True
    assert len(rec.calls) == 1


@needs_ffmpeg
def test_sdp_attrappe_als_jpeg_versucht_weder_sdp_noch_rtp_und_haengt_nicht(tmp_path, monkeypatch):
    """The review probe: an SDP text behind a JPEG magic. Free probing picks the sdp demuxer and
    opens rtp://; forced ``jpeg_pipe`` with ``-protocol_whitelist file`` only fails to decode."""
    reports = tmp_path / "rep"
    reports.mkdir()
    monkeypatch.setenv("FFREPORT", f"file={reports}/r-%p-%t.log:level=48")
    src = _files(tmp_path, bild_jpg=b"\xff\xd8\xff\n" + SDP)["bild_jpg"]
    box = outbox_mod.Outbox(tmp_path / "home", werkstatt=medien.Werkstatt(synchron=True, timeout=30))
    result = []
    t = threading.Thread(target=lambda: result.append(box.ingest(KEY_SESSION, [(str(src), False)], ["r"])),
                         daemon=True)
    t.start()
    t.join(60)
    assert not t.is_alive() and result
    meta = box.get(KEY_SESSION, result[0][0]["id"])
    assert meta["fassungen"]["klein"] == "fehler"
    text = "\n".join(p.read_text(errors="replace") for p in reports.iterdir())
    assert text, "ffmpeg wrote no report"
    assert "jpeg_pipe" in text
    assert "Format sdp" not in text and "[sdp" not in text and "'rtp'" not in text and "rtp://" not in text


# ── (2) opening the source: no FIFO hang, no symlinked directory into a denied place ─────────


def test_fifo_blockiert_nicht(tmp_path):
    fifo = tmp_path / "src" / "rohr.pdf"
    fifo.parent.mkdir()
    os.mkfifo(fifo)
    box = outbox_mod.Outbox(tmp_path / "home")
    result = []
    t = threading.Thread(target=lambda: result.append(box.ingest(KEY_SESSION, [(str(fifo), False)], ["r"])),
                         daemon=True)
    t.start()
    t.join(5)
    try:
        assert not t.is_alive(), "ingest hangs in open() of a FIFO"
        assert result == [[]]
        assert not list((tmp_path / "home").rglob("original"))
    finally:
        if t.is_alive():                      # release the blocked reader
            fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
            t.join(5)


def test_zwischen_symlink_auf_gesperrten_ort_wird_abgelehnt(tmp_path, monkeypatch):
    """``O_NOFOLLOW`` guards only the last component: a directory symlink in the middle that the
    gateway's filter saw pointing elsewhere (swap after the check) must not reach a denied file."""
    from gateway.platforms import base as gw_base
    geheim = tmp_path / "geheim"
    geheim.mkdir()
    (geheim / "schluessel.pdf").write_bytes(PDF)
    monkeypatch.setattr(gw_base, "_media_delivery_denied_paths", lambda: [geheim])
    (tmp_path / "zwischen").symlink_to(geheim, target_is_directory=True)
    box = outbox_mod.Outbox(tmp_path / "home")
    out = box.ingest(KEY_SESSION, [(str(tmp_path / "zwischen" / "schluessel.pdf"), False)], ["r"])
    assert out == []
    assert not list((tmp_path / "home").rglob("original"))
    # the same file outside the denied place still comes through
    (tmp_path / "frei").mkdir()
    (tmp_path / "frei" / "bericht.pdf").write_bytes(PDF)
    (tmp_path / "zwischen2").symlink_to(tmp_path / "frei", target_is_directory=True)
    [f] = box.ingest(KEY_SESSION, [(str(tmp_path / "zwischen2" / "bericht.pdf"), False)], ["r2"])
    assert f["id"] and f["size"] == len(PDF)


# ── (3) encode limits, prune after jobs, free disk ──────────────────────────────────────────


def test_jede_kodierung_mit_groessen_und_zeitgrenze(tmp_path, monkeypatch):
    rec = Recorder()
    box = _fake_box(tmp_path, monkeypatch, rec)
    src = _files(tmp_path, bild_png=PNG, film_mp4=FORMATE["film_mp4"][0], ton_ogg=FORMATE["ton_ogg"][0])
    for name in src:
        box.ingest(KEY_SESSION, [(str(src[name]), False)], [name])
    encodes = [c for c in rec.calls if "ffprobe" not in os.path.basename(c[0])]
    assert len(encodes) >= 6                              # klein, gross, poster, mp4, hls tier, m4a
    for cmd in encodes:
        last_in = max(i for i, a in enumerate(cmd) if a == "-i")
        out_opts = cmd[last_in + 2:]
        assert "-fs" in out_opts and out_opts[out_opts.index("-fs") + 1] == str(4 * outbox_mod.MAX_FILE), cmd
        assert "-t" in out_opts and out_opts[out_opts.index("-t") + 1] == str(medien.MAX_DAUER_S), cmd
    assert medien.MAX_DAUER_S == 7200


def test_ausgabe_an_der_groessengrenze_gilt_als_fehler(tmp_path, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(medien.subprocess, "run", rec)
    w = medien.Werkstatt(ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe", max_bytes=10)
    out = tmp_path / "o.webp"
    args = ["-protocol_whitelist", "file", "-f", "png_pipe", "-i", str(tmp_path / "x")]
    assert w.ffmpeg(args, out, fmt="webp") is False           # -fs cut it: a truncated file
    assert not out.exists() and not list(tmp_path.glob("*.part"))


def test_aufraeumen_auch_nach_erfolgreichem_job(tmp_path, monkeypatch):
    box = _fake_box(tmp_path, monkeypatch, Recorder())
    calls = []
    real = box.prune
    box.prune = lambda: calls.append(1) or real()
    src = _files(tmp_path, bild_png=PNG)
    box.ingest(KEY_SESSION, [(str(src["bild_png"]), False)], ["r"])
    assert len(calls) >= 3                                    # klein, gross, and the copy itself


def test_wenig_freier_platz_nur_original(tmp_path, monkeypatch):
    rec = Recorder()
    box = _fake_box(tmp_path, monkeypatch, rec, free_space=lambda _p: 2 * 1024 ** 3 - 1)
    src = _files(tmp_path, bild_png=PNG)
    [f] = box.ingest(KEY_SESSION, [(str(src["bild_png"]), False)], ["r"])
    assert f["fassungen"] == {} and "Speicher" in f["grund"] and rec.calls == []
    assert outbox_mod.MIN_FREE == 2 * 1024 ** 3
    ok = _fake_box(tmp_path / "b", monkeypatch, rec, free_space=lambda _p: 2 * 1024 ** 3)
    [g] = ok.ingest(KEY_SESSION, [(str(src["bild_png"]), False)], ["r"])
    assert g["fassungen"] == {"klein": "bereit", "gross": "bereit"}


# ── (4) a failed takeover is remembered; prune only after a real copy ───────────────────────


def test_fehlschlag_je_sitzung_und_pfad_wird_kurz_gemerkt(tmp_path):
    now = [1000.0]
    box = outbox_mod.Outbox(tmp_path / "home", clock=lambda: now[0], max_file=100)
    opened, pruned = [], []
    real_open, real_prune = box._open_source, box.prune
    box._open_source = lambda p: opened.append(p) or real_open(p)
    box.prune = lambda: pruned.append(1) or real_prune()
    gross = _files(tmp_path, gross_pdf=PDF)["gross_pdf"]
    weg = str(tmp_path / "src" / "gibts_nicht.pdf")
    first = box.ingest(KEY_SESSION, [(weg, False), (str(gross), False)], ["r"])
    assert [f["fehlt"] for f in first] == ["nicht_lesbar", "zu_gross"]
    assert len(opened) == 2 and pruned == []                  # nothing copied: no prune
    now[0] += 60
    again = box.ingest(KEY_SESSION, [(weg, False), (str(gross), False)], ["r"])
    assert again == first and len(opened) == 2 and pruned == []       # remembered, not re-read
    assert len(box.ingest("agent:main:andere", [(weg, False)], ["r"])) == 1 and len(opened) == 3
    now[0] += outbox_mod.NEGATIV_S
    box.ingest(KEY_SESSION, [(weg, False)], ["r"])
    assert len(opened) == 4                                   # after the window: tried again
    ok = _files(tmp_path, klein_pdf=PDF[:50])["klein_pdf"]
    box.ingest(KEY_SESSION, [(str(ok), False)], ["r"])
    assert pruned == [1]                                      # a real copy: prune


# ── (5) file transfers: own quota, streams and snapshots keep their room ────────────────────


def test_datei_uebertragungen_eigenes_kontingent_503_mit_retry_after(tmp_path):
    srv, fid = _one(tmp_path)
    try:
        app = srv.server
        assert app.max_file_transfers == 16
        path = session_path("files", fid, "original")
        assert _get(srv.port, path, srv.key)[0] == 200
        for _ in range(200):                                    # the server gives back after the last byte
            if app.open_file_transfers == 0:
                break
            threading.Event().wait(0.01)
        assert app.open_file_transfers == 0                    # given back after the transfer
        app.open_file_transfers = app.max_file_transfers      # all taken
        status, head, body = _get(srv.port, path, srv.key)
        assert status == 503 and head.get("Retry-After") and int(head["Retry-After"]) >= 1
        assert call(srv.port, "GET", session_path("snapshot"), srv.key)[0] == 200     # snapshot still served
        assert call(srv.port, "GET", session_path("files", fid), srv.key)[0] == 200   # metadata is no transfer
        app.open_file_transfers = 0
        assert _get(srv.port, path, srv.key)[0] == 200
    finally:
        srv.server.stop()

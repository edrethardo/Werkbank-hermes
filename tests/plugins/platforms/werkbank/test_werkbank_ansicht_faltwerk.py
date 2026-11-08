"""WB-1475: block ``faltwerk`` (``plugins/platforms/werkbank/ansichten/faltwerk.py``): 1 to 4 streams,
each a scene file (``scene.json`` of the Faltwerk viewer) plus its ``.falt`` sections, by reference."""

from __future__ import annotations

import json

import pytest

from ansichten_testkit import aufl, box, fehler_von, projekt  # noqa: F401 (fixtures)

from plugins.platforms.werkbank import ansichten

SZENE = {"fps": 30, "frames": 5,
         "sections": [{"url": "data/a.falt", "g0": 0, "nframes": 3}, {"url": "data/b.falt", "g0": 3, "nframes": 2}],
         "orbit": {"center": [0, 0.2, 0], "up": [0, 1, 0], "front": [0, 0, 1], "dist": 4, "elev_deg": 8, "fovy_deg": 40},
         "testcams": []}


@pytest.fixture
def szene(projekt):
    """A project folder with a scene file and two sections (the bytes are not checked by Hermes)."""
    ordner = projekt / "stream"
    ordner.mkdir()
    (ordner / "scene.json").write_text(json.dumps(SZENE), encoding="utf-8")
    (ordner / "a.falt").write_bytes(b"FALT" + b"\x00" * 40)
    (ordner / "b.csgs").write_bytes(b"CSGS" + b"\x00" * 40)
    return projekt


def stream(**extra):
    s = {"titel": "Standard", "szene": "stream/scene.json", "abschnitte": ["stream/a.falt", "stream/b.csgs"]}
    s.update(extra)
    return s


def aufruf(*streams, **extra):
    args = {"art": "faltwerk", "titel": "Vergleich", "daten": {"streams": list(streams)}}
    args.update(extra)
    return args


def test_the_block_is_found_but_stays_out_of_a_group():
    # the board's gruppe lists its blocks itself; a hidden WebGPU player in a tab would also run for nothing
    assert "faltwerk" in ansichten.arten()
    assert "faltwerk" not in ansichten.arten(in_gruppe=True)
    assert ansichten.bausteine()["faltwerk"].KURZ.startswith("faltwerk")
    inner = aufruf(stream())
    fehler = fehler_von({"art": "gruppe", "daten": {"tabs": [{"titel": "a", "ansicht": inner}]}})
    assert fehler == ["daten.tabs[0].ansicht.art: in einer gruppe stehen nur Bausteine (tabelle), keine faltwerk"]


def test_a_stream_resolves_to_chat_files(szene, box):
    view, fehler = ansichten.pruefen(aufruf(stream(), stream(titel="Klein")), aufl(szene, box))
    assert fehler == []
    assert view["art"] == "faltwerk" and view["titel"] == "Vergleich"
    s0, s1 = view["streams"]
    assert s0["titel"] == "Standard" and s1["titel"] == "Klein"
    assert s0["szene"]["datei"].startswith("fx_") and s0["szene"]["name"] == "scene.json"
    assert [a["name"] for a in s0["abschnitte"]] == ["a.falt", "b.csgs"]
    assert all(a["datei"].startswith("fx_") for a in s0["abschnitte"])
    assert view["optionen"] == {}


def test_the_option_compare_is_a_flag(szene, box):
    view, fehler = ansichten.pruefen(aufruf(stream(), stream(), optionen={"vergleich": False}), aufl(szene, box))
    assert fehler == [] and view["optionen"] == {"vergleich": False}
    assert fehler_von(aufruf(stream(), optionen={"vergleich": "ja"}), aufl(szene, box)) == [
        "optionen.vergleich: erwartet true oder false, gefunden Text „ja“"]
    assert fehler_von(aufruf(stream(), optionen={"farbe": 1}), aufl(szene, box))[0].startswith(
        "optionen.farbe: unbekanntes Feld")


def test_one_to_four_streams(szene, box):
    assert fehler_von(aufruf(), aufl(szene, box)) == [
        "daten.streams: erwartet Liste von 1 bis 4 Streams, gefunden Liste mit 0 Einträgen"]
    assert fehler_von(aufruf(*[stream()] * 5), aufl(szene, box)) == [
        "daten.streams: höchstens 4 Streams, gefunden 5"]
    assert fehler_von({"art": "faltwerk"}, aufl(szene, box)) == ["Aufruf: erwartet daten.streams, gefunden nichts"]
    assert fehler_von({"art": "faltwerk", "daten": {}}, aufl(szene, box)) == [
        "daten.streams: erwartet Liste von 1 bis 4 Streams, gefunden nichts"]
    assert fehler_von({"art": "faltwerk", "quelle": "x.json", "daten": {"streams": [stream()]}}, aufl(szene, box)) == [
        "quelle: ein faltwerk hat keine quelle, nur daten.streams"]
    assert fehler_von({"art": "faltwerk", "daten": {"streams": [stream()], "x": 1}}, aufl(szene, box)) == [
        "daten.x: unbekanntes Feld, erlaubt: streams"]


def test_a_stream_needs_scene_and_sections(szene, box):
    assert fehler_von(aufruf({"abschnitte": ["stream/a.falt"]}), aufl(szene, box)) == [
        "daten.streams[0].szene: erwartet Verweis (fx_…, up_…, /api/project-outbox/… oder Pfad im Projektordner), "
        "gefunden nichts"]
    assert fehler_von(aufruf({"szene": "stream/scene.json"}), aufl(szene, box)) == [
        "daten.streams[0].abschnitte: erwartet Liste von 1 bis 64 Verweisen auf .falt-Dateien, gefunden nichts"]
    assert fehler_von(aufruf(stream(farbe="rot")), aufl(szene, box))[0].startswith(
        "daten.streams[0].farbe: unbekanntes Feld")
    assert fehler_von(aufruf(stream(titel=5)), aufl(szene, box))[0].startswith("daten.streams[0].titel: erwartet Text")


def test_sections_are_falt_or_csgs_files(szene, box):
    (szene / "stream" / "c.png").write_bytes(b"\x89PNG" + b"\x00" * 20)
    bad = stream(abschnitte=["stream/a.falt", "stream/c.png"])
    assert fehler_von(aufruf(bad), aufl(szene, box)) == [
        "daten.streams[0].abschnitte[1]: „c.png“ ist keine .falt- oder .csgs-Datei"]
    assert fehler_von(aufruf(stream(szene="stream/a.falt")), aufl(szene, box))[0].startswith(
        "daten.streams[0].szene: „a.falt“ ist keine CSV- oder JSON-Datei")


def test_the_scene_file_is_checked_against_the_sections(szene, box):
    (szene / "stream" / "eine.json").write_text(json.dumps({**SZENE, "sections": SZENE["sections"][:1]}), encoding="utf-8")
    assert fehler_von(aufruf(stream(szene="stream/eine.json")), aufl(szene, box)) == [
        "daten.streams[0].abschnitte: die Szene nennt 1 Abschnitte, gefunden 2 Verweise"]
    (szene / "stream" / "kaputt.json").write_text("{nicht json", encoding="utf-8")
    assert fehler_von(aufruf(stream(szene="stream/kaputt.json")), aufl(szene, box))[0].startswith(
        "daten.streams[0].szene: kein gültiges JSON")
    (szene / "stream" / "leer.json").write_text(json.dumps({"fps": 30}), encoding="utf-8")
    assert fehler_von(aufruf(stream(szene="stream/leer.json")), aufl(szene, box))[0] == (
        "daten.streams[0].szene: erwartet sections (Liste von Abschnitten mit g0 und nframes), gefunden nichts")
    schlecht = {**SZENE, "sections": [{"url": "a", "g0": 0, "nframes": 0}, SZENE["sections"][1]]}
    (szene / "stream" / "null.json").write_text(json.dumps(schlecht), encoding="utf-8")
    assert fehler_von(aufruf(stream(szene="stream/null.json")), aufl(szene, box))[0].startswith(
        "daten.streams[0].szene.sections[0].nframes: erwartet ganze Zahl ≥ 1")
    kein_fps = {k: v for k, v in SZENE.items() if k != "fps"}
    (szene / "stream" / "fps.json").write_text(json.dumps(kein_fps), encoding="utf-8")
    assert fehler_von(aufruf(stream(szene="stream/fps.json")), aufl(szene, box))[0].startswith(
        "daten.streams[0].szene.fps: erwartet Zahl > 0")
    ohne_orbit = {k: v for k, v in SZENE.items() if k != "orbit"}
    (szene / "stream" / "orbit.json").write_text(json.dumps(ohne_orbit), encoding="utf-8")
    assert fehler_von(aufruf(stream(szene="stream/orbit.json")), aufl(szene, box))[0].startswith(
        "daten.streams[0].szene.orbit: erwartet Objekt {center, up, front, dist, elev_deg, fovy_deg}")
    negativ = {**SZENE, "sections": [{"url": "a", "g0": -1, "nframes": 3}, SZENE["sections"][1]]}
    (szene / "stream" / "g0.json").write_text(json.dumps(negativ), encoding="utf-8")
    assert fehler_von(aufruf(stream(szene="stream/g0.json")), aufl(szene, box))[0].startswith(
        "daten.streams[0].szene.sections[0].g0: erwartet Zahl ≥ 0")
    (szene / "stream" / "s.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    assert fehler_von(aufruf(stream(szene="stream/s.csv")), aufl(szene, box)) == [
        "daten.streams[0].szene: „s.csv“ ist keine JSON-Datei (scene.json des Players)"]


def test_references_stay_inside_the_chat(szene, box, tmp_path):
    outside = stream(abschnitte=["../geheim/x.csv"])
    assert "Projektordner" in fehler_von(aufruf(outside), aufl(szene, box))[0]
    (szene / "stream" / "link.falt").symlink_to(szene / "stream" / "a.falt")
    verlinkt = stream(abschnitte=["stream/link.falt", "stream/b.csgs"])
    assert "Symlink" in fehler_von(aufruf(verlinkt), aufl(szene, box))[0]
    assert "kein erlaubter Verweis" in fehler_von(aufruf(stream(szene="https://x.example/s.json")), aufl(szene, box))[0]


def test_without_a_resolver_nothing_resolves():
    assert fehler_von(aufruf(stream())) == ["daten.streams[0].szene: hier nicht auflösbar"]

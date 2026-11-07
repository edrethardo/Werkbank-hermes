"""WB-1473: block ``videos`` (``plugins/platforms/werkbank/ansichten/videos.py``): 1–9 videos by
reference (chat file, artefact, project folder), the compare option only for exactly two, nothing
that is not a video, no path outside the project folder."""

from __future__ import annotations

import os

import pytest

from ansichten_testkit import aufl, box, fehler_von  # noqa: F401 (fixtures)

from plugins.platforms.werkbank import ansichten

MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200


def videos(*quellen, **extra):
    args = {"art": "videos", "titel": "Vergleich",
            "daten": {"videos": [q if isinstance(q, dict) else {"quelle": q} for q in quellen]}}
    args.update(extra)
    return args


@pytest.fixture
def wurzel(tmp_path):
    root = tmp_path / "projekt"
    (root / "clips").mkdir(parents=True)
    for name in ("a.mp4", "b.mp4", "c.webm", "d.mov"):
        (root / "clips" / name).write_bytes(MP4)
    (root / "bild.png").write_bytes(PNG)
    (root / "tab.csv").write_text("a\n1\n", encoding="utf-8")
    outside = tmp_path / "geheim"
    outside.mkdir()
    (outside / "x.mp4").write_bytes(MP4)
    (root / "clips" / "link.mp4").symlink_to(outside / "x.mp4")
    (root / "clips" / "linkdir").symlink_to(outside, target_is_directory=True)
    return root


def test_two_videos_from_the_project_folder_are_normalised(wurzel, box):
    view, fehler = ansichten.pruefen(videos("clips/a.mp4", {"quelle": "clips/b.mp4", "titel": "neu"},
                                            optionen={"vergleich": True}), aufl(wurzel, box))
    assert fehler == []
    assert view["v"] == 1 and view["art"] == "videos" and view["titel"] == "Vergleich"
    assert [v["titel"] for v in view["videos"]] == ["", "neu"]
    assert [v["quelle"]["name"] for v in view["videos"]] == ["a.mp4", "b.mp4"]
    assert all(v["quelle"]["datei"].startswith("fx_") for v in view["videos"])      # copied into the outbox
    assert view["optionen"] == {"vergleich": True, "synchron": True, "schleife": False}


def test_defaults_and_options(wurzel, box):
    view, fehler = ansichten.pruefen(videos("clips/a.mp4", "clips/c.webm", "clips/d.mov",
                                            optionen={"synchron": False, "schleife": True}), aufl(wurzel, box))
    assert fehler == []
    assert view["optionen"] == {"vergleich": False, "synchron": False, "schleife": True}


def test_one_to_nine_videos(wurzel, box):
    ok = ["clips/a.mp4"] * 9
    assert ansichten.pruefen(videos(*ok), aufl(wurzel, box))[1] == []
    assert fehler_von(videos(*(ok + ["clips/b.mp4"])), aufl(wurzel, box)) == [
        "daten.videos: höchstens 9 Videos, gefunden 10"]
    assert fehler_von(videos(), aufl(wurzel, box)) == [
        "daten.videos: erwartet Liste von 1 bis 9 Videos, gefunden Liste mit 0 Einträgen"]


def test_what_is_not_a_video_is_refused_with_its_index(wurzel, box):
    fehler = fehler_von(videos("clips/a.mp4", "bild.png", "tab.csv"), aufl(wurzel, box))
    assert fehler == ["daten.videos[1].quelle: „bild.png“ ist kein Video (erlaubt: .mp4, .m4v, .mov, .webm)",
                      "daten.videos[2].quelle: „tab.csv“ ist kein Video (erlaubt: .mp4, .m4v, .mov, .webm)"]


def test_compare_needs_exactly_two(wurzel, box):
    assert fehler_von(videos("clips/a.mp4", optionen={"vergleich": True}), aufl(wurzel, box)) == [
        "optionen.vergleich: erwartet genau 2 Videos, gefunden 1"]
    assert fehler_von(videos("clips/a.mp4", "clips/b.mp4", "clips/c.webm", optionen={"vergleich": True}),
                      aufl(wurzel, box)) == ["optionen.vergleich: erwartet genau 2 Videos, gefunden 3"]
    assert fehler_von(videos("clips/a.mp4", "clips/b.mp4", optionen={"vergleich": "ja"}),
                      aufl(wurzel, box)) == ["optionen.vergleich: erwartet true oder false, gefunden Text „ja“"]


def test_unknown_fields_are_refused(wurzel, box):
    assert fehler_von(videos("clips/a.mp4", optionen={"html": "<b>"}), aufl(wurzel, box))[0].startswith(
        "optionen.html: unbekanntes Feld")
    args = videos("clips/a.mp4")
    args["daten"]["videos"][0]["stil"] = "x"
    assert fehler_von(args, aufl(wurzel, box))[0].startswith("daten.videos[0].stil: unbekanntes Feld")
    args = videos("clips/a.mp4")
    args["daten"]["skript"] = "x"
    assert fehler_von(args, aufl(wurzel, box))[0].startswith("daten.skript: unbekanntes Feld")


def test_data_is_required_and_quelle_is_not_a_video_list(wurzel, box):
    assert fehler_von({"art": "videos"}, aufl(wurzel, box)) == [
        "daten.videos: erwartet Liste von 1 bis 9 Videos, gefunden nichts"]
    assert fehler_von({"art": "videos", "quelle": "clips/a.mp4"}, aufl(wurzel, box)) == [
        "quelle: videos bekommt seine Verweise in daten.videos[].quelle, nicht in quelle"]
    assert fehler_von(videos({"titel": "ohne"}), aufl(wurzel, box)) == [
        "daten.videos[0].quelle: erwartet Verweis (fx_…, up_…, /api/project-outbox/… oder Pfad im Projektordner), gefunden nichts"]


def test_references_never_leave_the_project_folder(wurzel, box, tmp_path):
    fehler = fehler_von(videos("../geheim/x.mp4", "https://x.example/a.mp4", "clips/link.mp4", "clips/linkdir/x.mp4",
                               str(tmp_path / "geheim" / "x.mp4")), aufl(wurzel, box))
    assert len(fehler) == 5, fehler
    assert "kein „..“" in fehler[0] or "liegt nicht im Projektordner" in fehler[0]
    assert "kein erlaubter Verweis" in fehler[1]
    assert "Symlink" in fehler[2] and "Symlink" in fehler[3]
    assert "liegt nicht im Projektordner" in fehler[4]


def test_without_a_resolver_nothing_resolves():
    assert fehler_von(videos("clips/a.mp4")) == ["daten.videos[0].quelle: hier nicht auflösbar"]


def test_artefact_references(wurzel, box):
    art = "/api/project-outbox/projekt_a/lauf-1.mp4"
    view, fehler = ansichten.pruefen(videos(art, "https://board.example:8443" + art), aufl(wurzel, box))
    assert fehler == [] and view["videos"][0]["quelle"] == {"artefakt": art, "name": "lauf-1.mp4"}
    assert fehler_von(videos("/api/project-outbox/projekt_a/bild.png"), aufl(wurzel, box)) == [
        "daten.videos[0].quelle: „bild.png“ ist kein Video (erlaubt: .mp4, .m4v, .mov, .webm)"]


def test_titles_are_text_with_a_limit(wurzel, box):
    fehler = fehler_von(videos({"quelle": "clips/a.mp4", "titel": "x" * 201}), aufl(wurzel, box))
    assert fehler == ["daten.videos[0].titel: höchstens 200 Zeichen, gefunden 201"]
    fehler = fehler_von(videos({"quelle": "clips/a.mp4", "titel": 5}), aufl(wurzel, box))
    assert fehler[0].startswith("daten.videos[0].titel: erwartet Text")


def test_a_video_block_can_stand_in_a_group(wurzel, box):
    args = {"art": "gruppe", "titel": "Läufe", "daten": {"tabs": [
        {"titel": "A", "ansicht": videos("clips/a.mp4", "clips/b.mp4")}]}}
    view, fehler = ansichten.pruefen(args, aufl(wurzel, box))
    assert fehler == [], fehler
    assert view["tabs"][0]["ansicht"]["art"] == "videos"
    bad = {"art": "gruppe", "daten": {"tabs": [{"titel": "A", "ansicht": videos("bild.png")}]}}
    assert fehler_von(bad, aufl(wurzel, box))[0].startswith("daten.tabs[0].ansicht.daten.videos[0].quelle:")


def test_the_block_is_found_and_its_line_is_short():
    assert "videos" in ansichten.arten()
    mod = ansichten.bausteine()["videos"]
    assert mod.ART == "videos" and len(mod.KURZ) <= 110
    assert "videos" in ansichten.beschreibung()

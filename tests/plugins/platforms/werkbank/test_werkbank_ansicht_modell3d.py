"""WB-1474: block ``modell3d`` (``plugins/platforms/werkbank/ansichten/modell3d.py``): a 3D model or
point cloud (GLB, OBJ, PLY) by reference to a file, a few view options, never inside a ``gruppe``.
The file is only named here — the browser fetches it through the board's file routes."""

from __future__ import annotations

import pytest

from ansichten_testkit import aufl, box, fehler_von, projekt, tabelle  # noqa: F401 (fixtures)

from plugins.platforms.werkbank import ansichten

GLB = b"glTF\x02\x00\x00\x00" + b"\x00" * 64
PLY = b"ply\nformat ascii 1.0\nelement vertex 1\nproperty float x\nproperty float y\nproperty float z\nend_header\n0 0 0\n"
OBJ = b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n"


@pytest.fixture
def modelle(projekt):  # noqa: F811
    (projekt / "modelle").mkdir()
    (projekt / "modelle" / "stuhl.glb").write_bytes(GLB)
    (projekt / "modelle" / "wolke.ply").write_bytes(PLY)
    (projekt / "modelle" / "dreieck.obj").write_bytes(OBJ)
    (projekt / "modelle" / "notiz.txt").write_text("x", encoding="utf-8")
    (projekt / "modelle" / "szene.gltf").write_text("{}", encoding="utf-8")
    return projekt


def pruefe(args, projekt, box):  # noqa: F811
    return ansichten.pruefen({"art": "modell3d", **args}, aufl(projekt, box))


@pytest.mark.parametrize("pfad,format_", [("modelle/stuhl.glb", "glb"), ("modelle/wolke.ply", "ply"),
                                          ("modelle/dreieck.obj", "obj")])
def test_the_three_formats_by_file_name(modelle, box, pfad, format_):  # noqa: F811
    view, fehler = pruefe({"titel": "Modell", "quelle": pfad}, modelle, box)
    assert fehler == []
    assert view["art"] == "modell3d" and view["titel"] == "Modell"
    assert view["quelle"]["format"] == format_ and view["quelle"]["datei"].startswith("fx_")
    assert view["quelle"]["name"] == pfad.split("/")[1]
    assert view["optionen"] == {"hintergrund": "dunkel", "licht": 1.0, "punktgroesse": 2.0, "drehen": False}


def test_an_artefact_is_named_not_read(box):  # noqa: F811
    view, fehler = ansichten.pruefen({"art": "modell3d", "quelle": "/api/project-outbox/demo/scan.ply"},
                                     aufl(None, box))
    assert fehler == [] and view["quelle"] == {"artefakt": "/api/project-outbox/demo/scan.ply",
                                               "name": "scan.ply", "format": "ply"}


def test_options_are_checked_and_kept(modelle, box):  # noqa: F811
    view, _ = pruefe({"quelle": "modelle/stuhl.glb",
                      "optionen": {"hintergrund": "hell", "licht": 2, "punktgroesse": 5, "drehen": True}}, modelle, box)
    assert view["optionen"] == {"hintergrund": "hell", "licht": 2.0, "punktgroesse": 5.0, "drehen": True}


@pytest.mark.parametrize("optionen,meldung", [
    ({"hintergrund": "rot"}, "optionen.hintergrund: erwartet dunkel, hell oder grau, gefunden Text „rot“"),
    ({"licht": 9}, "optionen.licht: erwartet Zahl von 0 bis 3, gefunden Zahl 9"),
    ({"licht": "viel"}, "optionen.licht: erwartet Zahl von 0 bis 3, gefunden Text „viel“"),
    ({"licht": True}, "optionen.licht: erwartet Zahl von 0 bis 3, gefunden Wahrheitswert"),
    ({"punktgroesse": 0}, "optionen.punktgroesse: erwartet Zahl von 0.5 bis 20, gefunden Zahl 0"),
    ({"drehen": "ja"}, "optionen.drehen: erwartet true oder false, gefunden Text „ja“"),
    ({"farbe": "#f00"}, "optionen.farbe: unbekanntes Feld, erlaubt: hintergrund, licht, punktgroesse, drehen"),
])
def test_bad_options_name_field_expected_found(modelle, box, optionen, meldung):  # noqa: F811
    assert fehler_von({"art": "modell3d", "quelle": "modelle/stuhl.glb", "optionen": optionen},
                      aufl(modelle, box)) == [meldung]


def test_a_source_is_required_and_data_is_not_allowed(modelle, box):  # noqa: F811
    assert fehler_von({"art": "modell3d"}, aufl(modelle, box)) == [
        "quelle: erwartet Verweis auf eine .glb-, .obj- oder .ply-Datei, gefunden nichts"]
    assert fehler_von({"art": "modell3d", "daten": {"x": 1}, "quelle": "modelle/stuhl.glb"}, aufl(modelle, box)) == [
        "daten: ein modell3d hat keine daten, nur quelle (Datei) und optionen"]


@pytest.mark.parametrize("pfad", ["modelle/notiz.txt", "modelle/szene.gltf"])
def test_other_file_types_are_refused_with_the_allowed_ones(modelle, box, pfad):  # noqa: F811
    fehler = fehler_von({"art": "modell3d", "quelle": pfad}, aufl(modelle, box))
    assert len(fehler) == 1 and fehler[0].startswith("quelle: ") and ".glb, .obj, .ply" in fehler[0]
    assert not box._items                      # refused BEFORE a copy went into the outbox


def test_a_gltf_with_outside_files_is_not_offered_only_glb(modelle, box):  # noqa: F811
    fehler = fehler_von({"art": "modell3d", "quelle": "modelle/szene.gltf"}, aufl(modelle, box))
    assert "GLB" in fehler[0]


def test_the_project_rules_of_references_still_hold(modelle, box):  # noqa: F811
    assert "liegt nicht im Projektordner" in fehler_von(
        {"art": "modell3d", "quelle": "../geheim/x.glb"}, aufl(modelle, box))[0]
    assert "gibt es im Projektordner nicht" in fehler_von(
        {"art": "modell3d", "quelle": "modelle/fehlt.glb"}, aufl(modelle, box))[0]
    assert "kein erlaubter Verweis" in fehler_von(
        {"art": "modell3d", "quelle": "https://evil.example/x.glb"}, aufl(modelle, box))[0]


def test_not_inside_a_group_and_in_the_tool_description(modelle, box):  # noqa: F811
    assert "modell3d" in ansichten.arten() and "modell3d" not in ansichten.arten(in_gruppe=True)
    fehler = fehler_von({"art": "gruppe", "daten": {"tabs": [
        {"titel": "m", "ansicht": {"art": "modell3d", "quelle": "modelle/stuhl.glb"}}]}}, aufl(modelle, box))
    im_tab = ", ".join(ansichten.arten(in_gruppe=True))
    assert fehler == [f"daten.tabs[0].ansicht.art: in einer gruppe stehen nur Bausteine ({im_tab}), keine modell3d"]
    assert "modell3d" in ansichten.beschreibung()


def test_the_short_line_stays_within_its_budget():
    from plugins.platforms.werkbank.ansichten import modell3d
    try:
        import tiktoken
        n = len(tiktoken.get_encoding("cl100k_base").encode(modell3d.KURZ))
    except Exception:
        n = int(len(modell3d.KURZ) / 3.2) + 1
    assert n <= 40, n          # contract: ≤ 20 tokens of English; German text counts more per word

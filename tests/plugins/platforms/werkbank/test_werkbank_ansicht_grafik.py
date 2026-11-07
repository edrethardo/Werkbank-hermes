"""WB-1472: block ``grafik`` of the chat views (Chat-Ansichten, Schritt 2) — schema, limits, messages."""

from __future__ import annotations

import pytest

from ansichten_testkit import aufl, box, fehler_von, projekt  # noqa: F401 (fixtures)

from plugins.platforms.werkbank import ansichten


def grafik(**extra):
    args = {"art": "grafik", "titel": "PSNR je Lauf",
            "daten": {"x": {"titel": "Schritt", "einheit": "k"}, "y": {"titel": "PSNR", "einheit": "dB"},
                      "reihen": [{"name": "A", "punkte": [[1, 30.5], [2, 31], [3, None]]},
                                 {"name": "B", "punkte": [[1, 29], [2, 30.2], [3, 32]]}]}}
    args.update(extra)
    return args


def gueltig(args, a=None):
    view, fehler = ansichten.pruefen(args, a)
    assert fehler == [], fehler
    return view


def test_the_block_is_found_and_short():
    mod = ansichten.bausteine()["grafik"]
    assert mod.ART == "grafik" and mod.IN_GRUPPE is True
    assert int(len(mod.KURZ) / 3.2) + 1 <= 21          # contract: about 20 tokens


def test_a_valid_chart_keeps_series_axes_and_defaults():
    view = gueltig(grafik())
    assert view["art"] == "grafik" and view["v"] == 1 and view["quelle"] is None
    assert view["x"] == {"titel": "Schritt", "einheit": "k", "typ": "zahl", "spalte": None}
    assert view["y"] == {"titel": "PSNR", "einheit": "dB"}
    assert [r["name"] for r in view["reihen"]] == ["A", "B"]
    assert view["reihen"][0]["punkte"] == [[1, 30.5], [2, 31], [3, None]]
    assert view["optionen"] == {"form": "linie", "ausgeblendet": []}


@pytest.mark.parametrize("form", ["linie", "balken", "punkte", "flaeche"])
def test_every_form_is_accepted(form):
    view = gueltig(grafik(optionen={"form": form, "ausgeblendet": ["B"]}))
    assert view["optionen"] == {"form": form, "ausgeblendet": ["B"]}


def test_text_and_date_axes():
    args = grafik()
    args["daten"]["x"] = {"typ": "text"}
    args["daten"]["reihen"] = [{"name": "A", "punkte": [["Mo", 1], ["Di", 2]]}]
    assert gueltig(args)["x"]["typ"] == "text"
    args["daten"]["x"] = {"typ": "datum"}
    args["daten"]["reihen"] = [{"name": "A", "punkte": [["2026-11-06", 1], ["2026-11-07T10:00", 2]]}]
    assert gueltig(args)["x"]["typ"] == "datum"


def test_bad_values_name_field_expected_found():
    args = grafik()
    args["daten"]["reihen"][0]["punkte"][1] = ["x", 31]
    assert fehler_von(args)[0] == "daten.reihen[0].punkte[1][0]: erwartet Zahl, gefunden Text „x“"
    args = grafik()
    args["daten"]["reihen"][1]["punkte"][0] = [1, "viel"]
    assert fehler_von(args)[0] == "daten.reihen[1].punkte[0][1]: erwartet Zahl oder null, gefunden Text „viel“"
    args = grafik()
    args["daten"]["reihen"][0]["punkte"][0] = [1, float("inf")]
    assert "daten.reihen[0].punkte[0][1]: erwartet Zahl" in fehler_von(args)[0]
    args = grafik()
    args["daten"]["reihen"][0]["punkte"][0] = [1, True]
    assert "erwartet Zahl oder null, gefunden Wahrheitswert" in fehler_von(args)[0]
    args = grafik()
    args["daten"]["reihen"][0]["punkte"][0] = [1]
    assert fehler_von(args)[0] == "daten.reihen[0].punkte[0]: erwartet [x, y], gefunden Liste mit 1 Einträgen"
    args = grafik()
    args["daten"]["x"] = {"typ": "datum"}
    assert fehler_von(args)[0].startswith("daten.reihen[0].punkte[0][0]: erwartet Datum als ISO-Text")


def test_schema_errors():
    assert fehler_von(grafik(optionen={"form": "torte"})) == [
        "optionen.form: erwartet linie oder balken oder punkte oder flaeche, gefunden Text „torte“"]
    assert fehler_von(grafik(optionen={"ausgeblendet": ["Z"]})) == [
        "optionen.ausgeblendet: erwartet Reihen-Namen aus A, B, gefunden Text „Z“"]
    assert fehler_von(grafik(optionen={"farbe": "rot"}))[0].startswith("optionen.farbe: unbekanntes Feld")
    args = grafik()
    args["daten"]["reihen"][1]["name"] = "A"
    assert fehler_von(args) == ["daten.reihen[1].name: „A“ kommt doppelt vor"]
    args = grafik()
    args["daten"]["reihen"] = []
    assert fehler_von(args)[0].startswith("daten.reihen: erwartet Liste von Reihen (1 bis 12)")
    args = grafik()
    args["daten"]["y"] = {"einheit": "<b>dB</b>" * 20}
    assert "daten.y.einheit: höchstens 20 Zeichen" in fehler_von(args)[0]
    args = grafik()
    args["daten"]["x"] = {"typ": "kreis"}
    assert fehler_von(args)[0] == "daten.x.typ: erwartet zahl oder text oder datum, gefunden Text „kreis“"
    args = grafik()
    args["daten"]["reihen"][0]["farbe"] = "rot"
    assert fehler_von(args)[0].startswith("daten.reihen[0].farbe: unbekanntes Feld, erlaubt: name, punkte")
    assert fehler_von({"art": "grafik"}) == ["Aufruf: erwartet daten oder quelle, gefunden keins von beiden"]


def test_limits():
    args = grafik()
    args["daten"]["reihen"] = [{"name": f"r{i}", "punkte": [[1, 1]]} for i in range(13)]
    assert fehler_von(args) == ["daten.reihen: höchstens 12 Reihen, gefunden 13"]
    args = grafik()
    args["daten"]["reihen"][0]["punkte"] = [[i, i] for i in range(2001)]
    assert fehler_von(args) == ["daten.reihen[0].punkte: höchstens 2000 Punkte, gefunden 2001"]
    args = grafik()
    args["daten"]["reihen"] = [{"name": "A", "punkte": []}]
    assert fehler_von(args)[0].startswith("daten.reihen[0].punkte: erwartet Liste von Punkten")


def test_the_group_check_is_the_frames_not_this_blocks():
    """A chart inside a gruppe tab passes Hermes; the board's gruppe.js lists its blocks itself
    (``GRUPPE_BAUSTEINE``, shared file) and has to learn ``grafik`` — see the WB-1472 report."""
    gruppe = {"art": "gruppe", "titel": "G", "daten": {"tabs": [{"titel": "t", "ansicht": grafik()}]}}
    view, fehler = ansichten.pruefen(gruppe)
    assert fehler == [] and view["tabs"][0]["ansicht"]["art"] == "grafik"


# -- quelle ------------------------------------------------------------------------------------------
def quelle_args(datei, **daten):
    d = {"x": {"spalte": "t"}, "reihen": [{"name": "PSNR", "spalte": "psnr"}]}
    d.update(daten)
    return {"art": "grafik", "quelle": datei, "daten": d}


def test_a_csv_source_is_checked_and_kept_by_reference(projekt, box):  # noqa: F811
    (projekt / "daten" / "kurve.csv").write_text("t,psnr,ssim\n1,30,0.9\n2,31,0.91\n", encoding="utf-8")
    view = gueltig(quelle_args("daten/kurve.csv"), aufl(projekt, box))
    assert view["quelle"]["format"] == "csv" and view["quelle"]["name"] == "kurve.csv"
    assert view["x"]["spalte"] == "t"
    assert view["reihen"] == [{"name": "PSNR", "spalte": "psnr", "punkte": None}]


def test_a_json_source_is_checked(projekt, box):  # noqa: F811
    (projekt / "daten" / "k.json").write_text('[{"t": 1, "psnr": 3}, {"t": 2, "psnr": 4}]', encoding="utf-8")
    assert gueltig(quelle_args("daten/k.json"), aufl(projekt, box))["quelle"]["format"] == "json"


def test_source_errors(projekt, box):  # noqa: F811
    (projekt / "daten" / "kurve.csv").write_text("t,psnr\n1,30\n", encoding="utf-8")
    a = aufl(projekt, box)
    assert fehler_von(quelle_args("daten/kurve.csv", reihen=[{"name": "X", "spalte": "fehlt"}]), a) == [
        "quelle: Spalte „fehlt“ fehlt in der Datei (vorhanden: t, psnr)"]
    assert fehler_von(quelle_args("daten/kurve.csv", x={"spalte": "zeit"}), a) == [
        "quelle: Spalte „zeit“ fehlt in der Datei (vorhanden: t, psnr)"]
    assert fehler_von({"art": "grafik", "quelle": "daten/kurve.csv"}, a)[0].startswith(
        "daten.x.spalte: erwartet")
    both = quelle_args("daten/kurve.csv")
    both["daten"]["reihen"][0]["punkte"] = [[1, 2]]
    assert fehler_von(both, a) == ["daten.reihen[0].punkte: entweder punkte oder quelle, nicht beides"]
    inline = grafik(quelle="daten/kurve.csv")
    assert fehler_von(inline, a)[0] == "daten.reihen[0].punkte: entweder punkte oder quelle, nicht beides"
    (projekt / "daten" / "viel.csv").write_text("t,psnr\n" + "1,2\n" * 5001, encoding="utf-8")
    assert fehler_von(quelle_args("daten/viel.csv"), a) == ["quelle: höchstens 5000 Zeilen, die Datei hat 5001"]
    assert fehler_von(quelle_args("daten/kurve.csv"), None) == ["quelle: hier nicht auflösbar"]

"""WB-1471: block ``tabelle`` (``plugins/platforms/werkbank/ansichten/tabelle.py``): schema, cells,
links only http/https, ≤ 500 rows, ≤ 50 columns, image cells and data files by reference."""

from __future__ import annotations

import json

import pytest

from ansichten_testkit import aufl, box, fehler_von, projekt, tabelle  # noqa: F401 (fixtures)

from plugins.platforms.werkbank import ansichten


def test_a_valid_table_is_normalised():
    view, fehler = ansichten.pruefen(tabelle(optionen={"sortiere": {"spalte": "dauer", "absteigend": True}}))
    assert fehler == []
    assert view["v"] == 1 and view["art"] == "tabelle" and view["titel"] == "Läufe"
    assert [s["typ"] for s in view["spalten"]] == ["text", "zahl", "status"]
    assert view["spalten"][1]["titel"] == "dauer"           # titel defaults to the id
    assert view["zeilen"] == [["a", 1.5, "ok"], ["b", 2, "fehler"]]   # objects become lists
    assert view["optionen"] == {"sortiere": {"spalte": "dauer", "absteigend": True}}


def test_a_wrong_cell_names_field_expected_and_found():
    args = tabelle()
    args["daten"]["zeilen"][0][1] = "abc"
    assert fehler_von(args) == ["daten.zeilen[0][1]: erwartet Zahl, gefunden Text „abc“"]


def test_every_wrong_cell_is_named_and_the_text_is_capped():
    args = tabelle()
    args["daten"]["zeilen"] = [["a", "x", "ok"]] * 8
    fehler = fehler_von(args)
    assert len(fehler) == 8
    text = ansichten.fehlertext(fehler)
    assert text.startswith("Ansicht nicht gezeigt. daten.zeilen[0][1]: erwartet Zahl")
    assert "(und 3 weitere)" in text and text.endswith("rufe ansicht_zeigen erneut auf.")


def test_unknown_options_are_refused():
    assert fehler_von(tabelle(optionen={"html": "<b>"}))[0].startswith("optionen.html: unbekanntes Feld")


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,x", "//evil", "http://a b", "ftp://x"])
def test_links_only_http_and_https(url):
    args = {"art": "tabelle", "daten": {"spalten": [{"id": "l", "typ": "link"}], "zeilen": [[url]]}}
    assert fehler_von(args) == [f"daten.zeilen[0][0]: erwartet Link mit http:// oder https://, gefunden Text „{url}“"]


def test_links_with_text_and_dates():
    args = {"art": "tabelle", "daten": {"spalten": [{"id": "l", "typ": "link"}, {"id": "d", "typ": "datum"}],
                                        "zeilen": [[{"text": "Doku", "url": "https://x.example/a"}, "2026-11-07"],
                                                   ["http://y.example", "2026-11-07T14:30Z"]]}}
    view, fehler = ansichten.pruefen(args)
    assert fehler == []
    assert view["zeilen"][0][0] == {"text": "Doku", "url": "https://x.example/a"}
    assert view["zeilen"][1][0] == {"text": "", "url": "http://y.example"}
    bad = {"art": "tabelle", "daten": {"spalten": [{"id": "d", "typ": "datum"}], "zeilen": [["7.11.2026"]]}}
    assert fehler_von(bad)[0].startswith("daten.zeilen[0][0]: erwartet Datum als ISO-Text")


def test_at_most_500_rows():
    args = {"art": "tabelle", "daten": {"spalten": ["n"], "zeilen": [[i] for i in range(501)]}}
    assert fehler_von(args) == ["daten.zeilen: höchstens 500 Zeilen, gefunden 501"]
    args["daten"]["zeilen"].pop()
    assert ansichten.pruefen(args)[1] == []


def test_at_most_50_columns():
    args = {"art": "tabelle", "daten": {"spalten": [f"s{i}" for i in range(51)], "zeilen": []}}
    assert fehler_von(args) == ["daten.spalten: höchstens 50 Spalten, gefunden 51"]


def test_image_cells_resolve_to_board_references(projekt, box):  # noqa: F811
    args = {"art": "tabelle", "daten": {"spalten": [{"id": "b", "typ": "bild"}], "zeilen": [["bild.png"], ["bild.png"]]}}
    view, fehler = ansichten.pruefen(args, aufl(projekt, box))
    assert fehler == []
    assert view["zeilen"][0][0]["datei"] == view["zeilen"][1][0]["datei"]       # one copy per image
    assert "url" not in json.dumps(view)
    bad = {"art": "tabelle", "daten": {"spalten": [{"id": "b", "typ": "bild"}], "zeilen": [["https://x.example/a.png"]]}}
    assert fehler_von(bad, aufl(projekt, box))[0].startswith("daten.zeilen[0][0]: „https://x.example/a.png“ ist kein erlaubter")
    assert fehler_von({"art": "tabelle", "daten": {"spalten": [{"id": "b", "typ": "bild"}],
                                                   "zeilen": [["daten/tab.csv"]]}}, aufl(projekt, box)) == [
        "daten.zeilen[0][0]: „tab.csv“ ist kein Bild (erlaubt: .png, .jpg, .jpeg, .gif, .webp)"]


def test_a_data_file_is_checked_against_the_limits(projekt, box):  # noqa: F811
    (projekt / "gross.csv").write_text("n\n" + "1\n" * 501, encoding="utf-8")
    assert fehler_von({"art": "tabelle", "quelle": "gross.csv"}, aufl(projekt, box)) == [
        "quelle: höchstens 500 Zeilen, die Datei hat 501"]
    assert fehler_von({"art": "tabelle", "quelle": "daten/tab.csv", "daten": {"spalten": [{"id": "fehlt"}]}},
                      aufl(projekt, box)) == ["quelle: Spalte „fehlt“ fehlt in der Datei (vorhanden: name, dauer)"]
    (projekt / "kaputt.json").write_text("[1,", encoding="utf-8")
    assert fehler_von({"art": "tabelle", "quelle": "kaputt.json"}, aufl(projekt, box))[0].startswith(
        "quelle: kein gültiges JSON")

"""WB-1471: block ``gruppe`` (``plugins/platforms/werkbank/ansichten/gruppe.py``): tabs of blocks,
≤ 8 tabs, never a group in a group, messages of a tab's block name the full path."""

from __future__ import annotations

from ansichten_testkit import fehler_von, tabelle

from plugins.platforms.werkbank import ansichten


def test_a_group_with_tabs():
    view, fehler = ansichten.pruefen({"art": "gruppe", "titel": "Vergleich",
                                      "daten": {"tabs": [{"titel": "A", "ansicht": tabelle()},
                                                         {"titel": "B", "ansicht": tabelle(titel="B")}]}})
    assert fehler == []
    assert view["art"] == "gruppe" and view["titel"] == "Vergleich"
    assert [t["titel"] for t in view["tabs"]] == ["A", "B"]
    assert view["tabs"][1]["ansicht"]["art"] == "tabelle" and view["tabs"][1]["ansicht"]["titel"] == "B"
    bad = fehler_von({"art": "gruppe", "daten": {"tabs": [{"titel": "A", "ansicht": {"art": "tabelle", "daten": {
        "spalten": ["a"], "zeilen": [[True]]}}}]}})
    assert bad == ["daten.tabs[0].ansicht.daten.zeilen[0][0]: erwartet Text, gefunden Wahrheitswert"]


def test_at_most_8_tabs():
    tab = {"titel": "t", "ansicht": tabelle()}
    assert fehler_von({"art": "gruppe", "daten": {"tabs": [tab] * 9}}) == ["daten.tabs: höchstens 8 Tabs, gefunden 9"]


def test_a_group_holds_blocks_never_a_group():
    inner = {"art": "gruppe", "daten": {"tabs": [{"titel": "x", "ansicht": tabelle()}]}}
    fehler = fehler_von({"art": "gruppe", "daten": {"tabs": [{"titel": "a", "ansicht": inner}]}})
    im_tab = ", ".join(ansichten.arten(in_gruppe=True))            # derived: every block that allows it
    assert fehler == [f"daten.tabs[0].ansicht.art: in einer gruppe stehen nur Bausteine ({im_tab}), keine gruppe"]


def test_a_tab_needs_a_title_and_a_group_no_source():
    assert fehler_von({"art": "gruppe", "daten": {"tabs": [{"ansicht": tabelle()}]}}) == [
        "daten.tabs[0].titel: erwartet Text (höchstens 200 Zeichen), gefunden nichts"]
    assert fehler_von({"art": "gruppe", "quelle": "x.csv", "daten": {"tabs": []}}) == [
        "quelle: eine gruppe hat keine quelle, nur daten.tabs"]

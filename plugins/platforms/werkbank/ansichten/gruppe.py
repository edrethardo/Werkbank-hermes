"""WB-1471: block ``gruppe`` of the chat views (contract: ``README.md`` in this folder).

A heading and tabs, each tab holds ONE block (never a gruppe). ≤ 8 tabs.
"""

from __future__ import annotations

from typing import Any, Dict

from . import Fehler, Kontext, erwartet, nur_felder, titel

ART = "gruppe"
IN_GRUPPE = False
KURZ = "gruppe: daten={tabs:[{titel,ansicht:{art,…}}]} ≤ 8, keine gruppe darin."

MAX_TABS = 8


def pruefen(args: Dict[str, Any], feld: str, k: Kontext) -> Dict[str, Any]:
    if args.get("quelle") is not None:
        raise Fehler(f"{feld}quelle: eine gruppe hat keine quelle, nur daten.tabs")
    if args.get("optionen") not in (None, {}):
        raise Fehler(f"{feld}optionen: eine gruppe hat keine optionen")
    daten = args.get("daten")
    if not isinstance(daten, dict):
        raise erwartet(f"{feld}daten", "Objekt {tabs}", daten)
    nur_felder(daten, f"{feld}daten", ("tabs",))
    tabs = daten.get("tabs")
    if not isinstance(tabs, list) or not tabs:
        raise erwartet(f"{feld}daten.tabs", f"Liste von Tabs (1 bis {MAX_TABS})", tabs)
    if len(tabs) > MAX_TABS:
        raise Fehler(f"{feld}daten.tabs: höchstens {MAX_TABS} Tabs, gefunden {len(tabs)}")
    out_tabs = []
    for i, tab in enumerate(tabs):
        f = f"{feld}daten.tabs[{i}]"
        if not isinstance(tab, dict):
            raise erwartet(f, "Objekt {titel, ansicht}", tab)
        nur_felder(tab, f, ("titel", "ansicht"))
        out_tabs.append({"titel": titel(tab.get("titel"), f + ".titel", pflicht=True),
                         "ansicht": k.baustein(tab.get("ansicht"), f + ".ansicht.")})
    return {"tabs": out_tabs}

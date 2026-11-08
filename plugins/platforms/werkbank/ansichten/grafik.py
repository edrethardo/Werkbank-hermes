"""WB-1472: block ``grafik`` of the chat views (contract: ``README.md`` in this folder).

Forms linie, balken, punkte, flaeche; several series; axes with a unit. Series inline
(``daten.reihen[].punkte``: ``[[x, y], …]``) or by ``quelle`` (CSV/JSON file: one column for x, one per
series). Limits: ≤ 12 series, ≤ 2000 points per series, ≤ 5000 rows in a source file.
"""

from __future__ import annotations

import csv
import io
import json
import math
from typing import Any, Dict, List, Optional

from . import Fehler, Kontext, MAX_QUELLE, erwartet, iso_datum, nur_felder

ART = "grafik"
IN_GRUPPE = True
KURZ = "grafik: daten={reihen:[{name,punkte:[[x,y]]}]}, optionen={form}"

FORMEN = ("linie", "balken", "punkte", "flaeche")
X_TYPEN = ("zahl", "text", "datum")
MAX_REIHEN = 12
MAX_PUNKTE = 2000
MAX_ZEILEN = 5000
MAX_NAME = 60
MAX_ACHSE_TITEL = 60
MAX_EINHEIT = 20
MAX_KATEGORIE = 100


def _text(value: Any, feld: str, limit: int, *, was_ist: str = "Text") -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise erwartet(feld, f"{was_ist} (höchstens {limit} Zeichen)", value)
    if len(value) > limit:
        raise Fehler(f"{feld}: höchstens {limit} Zeichen, gefunden {len(value)}")
    return "".join(" " if ord(c) < 32 or ord(c) == 127 else c for c in value).strip()


def _achse(value: Any, feld: str, *, x: bool, quelle: bool) -> Dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise erwartet(feld, "Objekt {titel?, einheit?" + (", typ?" + (", spalte" if quelle else "") if x else "") + "}", value)
    nur_felder(value, feld, ("titel", "einheit") + (("typ", "spalte") if x else ()))
    out: Dict[str, Any] = {"titel": _text(value.get("titel"), feld + ".titel", MAX_ACHSE_TITEL),
                           "einheit": _text(value.get("einheit"), feld + ".einheit", MAX_EINHEIT)}
    if x:
        typ = value.get("typ", "zahl")
        if typ not in X_TYPEN:
            raise erwartet(feld + ".typ", " oder ".join(X_TYPEN), typ)
        out["typ"] = typ
        spalte = value.get("spalte")
        if quelle:
            if not isinstance(spalte, str) or not spalte.strip():
                raise erwartet(feld + ".spalte", "Spalten-Name der Datei", spalte)
        elif spalte is not None:
            raise Fehler(f"{feld}.spalte: nur mit quelle")
        out["spalte"] = spalte if quelle else None
    return out


def _x_wert(value: Any, typ: str, feld: str) -> Any:
    if typ == "zahl":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise erwartet(feld, "Zahl", value)
        return value
    if typ == "datum":
        if not isinstance(value, str) or not iso_datum(value):
            raise erwartet(feld, "Datum als ISO-Text (2026-11-07 oder 2026-11-07T14:30)", value)
        return value
    if not isinstance(value, str) or not value or len(value) > MAX_KATEGORIE:
        raise erwartet(feld, f"Text (1 bis {MAX_KATEGORIE} Zeichen)", value)
    return value


def _y_wert(value: Any, feld: str) -> Any:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise erwartet(feld, "Zahl oder null", value)
    return value


def _punkte(value: Any, typ: str, feld: str) -> List[List[Any]]:
    if not isinstance(value, list) or not value:
        raise erwartet(feld, f"Liste von Punkten [x, y] (1 bis {MAX_PUNKTE})", value)
    if len(value) > MAX_PUNKTE:
        raise Fehler(f"{feld}: höchstens {MAX_PUNKTE} Punkte, gefunden {len(value)}")
    out = []
    for i, p in enumerate(value):
        f = f"{feld}[{i}]"
        if not isinstance(p, list) or len(p) != 2:
            raise erwartet(f, "[x, y]", p)
        out.append([_x_wert(p[0], typ, f + "[0]"), _y_wert(p[1], f + "[1]")])
    return out


def _reihen(value: Any, typ: str, feld: str, *, quelle: bool) -> List[Dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise erwartet(feld, f"Liste von Reihen (1 bis {MAX_REIHEN})", value)
    if len(value) > MAX_REIHEN:
        raise Fehler(f"{feld}: höchstens {MAX_REIHEN} Reihen, gefunden {len(value)}")
    out, seen = [], set()
    for i, reihe in enumerate(value):
        f = f"{feld}[{i}]"
        if not isinstance(reihe, dict):
            raise erwartet(f, "Objekt {name, punkte}" if not quelle else "Objekt {name?, spalte}", reihe)
        nur_felder(reihe, f, ("name", "punkte") if not quelle else ("name", "spalte"))
        spalte = None
        if quelle:
            spalte = reihe.get("spalte")
            if not isinstance(spalte, str) or not spalte.strip():
                raise erwartet(f + ".spalte", "Spalten-Name der Datei", spalte)
        name = _text(reihe.get("name"), f + ".name", MAX_NAME) or (spalte or "")
        if not name:
            raise erwartet(f + ".name", f"Text (1 bis {MAX_NAME} Zeichen)", reihe.get("name"))
        if name in seen:
            raise Fehler(f"{f}.name: „{name}“ kommt doppelt vor")
        seen.add(name)
        punkte = None if quelle else _punkte(reihe.get("punkte"), typ, f + ".punkte")
        out.append({"name": name, "spalte": spalte, "punkte": punkte})
    return out


def _optionen(value: Any, namen: List[str], feld: str) -> Dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise erwartet(feld, "Objekt {form?, ausgeblendet?}", value)
    nur_felder(value, feld, ("form", "ausgeblendet"))
    form = value.get("form", "linie")
    if form not in FORMEN:
        raise erwartet(feld + ".form", " oder ".join(FORMEN), form)
    aus = value.get("ausgeblendet", [])
    if not isinstance(aus, list):
        raise erwartet(feld + ".ausgeblendet", "Liste von Reihen-Namen", aus)
    for name in aus:
        if name not in namen:
            raise erwartet(feld + ".ausgeblendet", f"Reihen-Namen aus {', '.join(namen)}", name)
    return {"form": form, "ausgeblendet": list(dict.fromkeys(aus))}


def _quelle_pruefen(data: Optional[bytes], ref: Dict[str, Any], spalten: List[str], feld: str) -> None:
    """Limits of a data file the plugin could read (an artefact is checked by the board)."""
    if data is None:
        return
    if len(data) > MAX_QUELLE:
        raise Fehler(f"{feld}: die Datei ist größer als {MAX_QUELLE // 1024} KB")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Fehler(f"{feld}: die Datei ist kein UTF-8-Text") from None
    header: Optional[List[Any]]
    if str(ref.get("name") or "").lower().endswith(".json"):
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            raise Fehler(f"{feld}: kein gültiges JSON ({exc.msg}, Zeile {exc.lineno})") from None
        rows = doc.get("zeilen") if isinstance(doc, dict) else doc
        if not isinstance(rows, list):
            raise erwartet(feld, "JSON-Liste von Zeilen oder {spalten, zeilen}", doc)
        header = None
        if rows and all(isinstance(r, dict) for r in rows):
            header = list(dict.fromkeys(k for r in rows for k in r))
        elif isinstance(doc, dict) and isinstance(doc.get("spalten"), list):
            header = [s.get("id") if isinstance(s, dict) else s for s in doc["spalten"]]
    else:
        try:
            parsed = list(csv.reader(io.StringIO(text)))
        except csv.Error as exc:
            raise Fehler(f"{feld}: CSV nicht lesbar ({exc})") from None
        if not parsed:
            raise Fehler(f"{feld}: die CSV-Datei ist leer")
        header, rows = parsed[0], parsed[1:]
    if len(rows) > MAX_ZEILEN:
        raise Fehler(f"{feld}: höchstens {MAX_ZEILEN} Zeilen, die Datei hat {len(rows)}")
    if header is not None:
        for s in spalten:
            if s not in header:
                raise Fehler(f"{feld}: Spalte „{s}“ fehlt in der Datei (vorhanden: {', '.join(map(str, header[:10]))})")


def pruefen(args: Dict[str, Any], feld: str, k: Kontext) -> Dict[str, Any]:
    daten, quelle = args.get("daten"), args.get("quelle")
    if daten is None and quelle is None:
        raise Fehler(f"{feld or 'Aufruf'}: erwartet daten oder quelle, gefunden keins von beiden")
    if daten is not None and not isinstance(daten, dict):
        raise erwartet(f"{feld}daten", "Objekt {x?, y?, reihen}", daten)
    daten = daten or {}
    nur_felder(daten, f"{feld}daten", ("x", "y", "reihen"))
    mit_quelle = quelle is not None
    if mit_quelle and isinstance(daten.get("reihen"), list):
        for i, r in enumerate(daten["reihen"]):
            if isinstance(r, dict) and "punkte" in r:
                raise Fehler(f"{feld}daten.reihen[{i}].punkte: entweder punkte oder quelle, nicht beides")
    x = _achse(daten.get("x"), f"{feld}daten.x", x=True, quelle=mit_quelle)
    y = _achse(daten.get("y"), f"{feld}daten.y", x=False, quelle=mit_quelle)
    reihen = _reihen(daten.get("reihen"), x["typ"], f"{feld}daten.reihen", quelle=mit_quelle)
    out: Dict[str, Any] = {"x": x, "y": y, "reihen": reihen, "quelle": None}
    if mit_quelle:
        if k.aufl is None:
            raise Fehler(f"{feld}quelle: hier nicht auflösbar")
        ref, data = k.aufl.loese(quelle, f"{feld}quelle", daten=True)
        _quelle_pruefen(data, ref, [x["spalte"]] + [r["spalte"] for r in reihen], f"{feld}quelle")
        ref["format"] = "json" if str(ref.get("name") or "").lower().endswith(".json") else "csv"
        out["quelle"] = ref
    out["optionen"] = _optionen(args.get("optionen"), [r["name"] for r in reihen], f"{feld}optionen")
    return out

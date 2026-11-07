"""WB-1471: block ``tabelle`` of the chat views (contract: ``README.md`` in this folder).

Columns with a type (text, zahl, datum, link, status, bild), rows inline (≤ 500) or ``quelle``
(a CSV/JSON file, by reference). Limits: ≤ 500 rows, ≤ 50 columns, a text cell ≤ 2000 characters.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from typing import Any, Dict, List, Optional

from . import Fehler, Kontext, MAX_QUELLE, erwartet, iso_datum, link, nur_felder, titel

ART = "tabelle"
IN_GRUPPE = True
KURZ = ("art=tabelle: daten={spalten:[{id,titel?,typ?}], zeilen:[[...]] oder [{id:wert}]}, "
        "typ text|zahl|datum|link|status|bild; link: http(s)-URL oder {text,url}; statt zeilen "
        "quelle (CSV/JSON); optionen={sortiere:{spalte,absteigend?}, ausgeblendet:[id]}; ≤ 500 Zeilen.")

TYPEN = ("text", "zahl", "datum", "link", "status", "bild")
MAX_ZEILEN = 500
MAX_SPALTEN = 50
MAX_TEXT = 2000
MAX_STATUS = 40
MAX_SPALTEN_ID = 64
MAX_FEHLER_SAMMELN = 20
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _zelle(value: Any, typ: str, feld: str, k: Kontext) -> Any:
    if value is None:
        return None
    if typ == "text":
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise erwartet(feld, "Text", value)
        text = value if isinstance(value, str) else str(value)
        if len(text) > MAX_TEXT:
            raise Fehler(f"{feld}: höchstens {MAX_TEXT} Zeichen, gefunden {len(text)}")
        return text
    if typ == "zahl":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise erwartet(feld, "Zahl", value)
        return value
    if typ == "datum":
        if not isinstance(value, str) or not iso_datum(value):
            raise erwartet(feld, "Datum als ISO-Text (2026-11-07 oder 2026-11-07T14:30)", value)
        return value
    if typ == "link":
        if isinstance(value, dict):
            nur_felder(value, feld, ("text", "url"))
            text = value.get("text")
            if text is not None and not isinstance(text, str):
                raise erwartet(feld + ".text", "Text", text)
            return {"text": (text or "")[:MAX_TEXT], "url": link(value.get("url"), feld + ".url")}
        return {"text": "", "url": link(value, feld)}
    if typ == "status":
        if not isinstance(value, str):
            raise erwartet(feld, f"Status-Wort (Text, höchstens {MAX_STATUS} Zeichen)", value)
        if len(value) > MAX_STATUS:
            raise Fehler(f"{feld}: Status höchstens {MAX_STATUS} Zeichen, gefunden {len(value)}")
        return value
    if typ == "bild":
        if k.aufl is None:
            raise Fehler(f"{feld}: Bilder brauchen einen Verweis, der hier nicht auflösbar ist")
        return k.aufl.bild(value, feld)
    raise Fehler(f"{feld}: unbekannter Typ {typ}")


def _spalten(value: Any, feld: str) -> List[Dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise erwartet(feld, f"Liste von Spalten (1 bis {MAX_SPALTEN})", value)
    if len(value) > MAX_SPALTEN:
        raise Fehler(f"{feld}: höchstens {MAX_SPALTEN} Spalten, gefunden {len(value)}")
    out, seen = [], set()
    for i, spalte in enumerate(value):
        f = f"{feld}[{i}]"
        if isinstance(spalte, str):
            spalte = {"id": spalte}
        if not isinstance(spalte, dict):
            raise erwartet(f, "Objekt {id, titel?, typ?}", spalte)
        nur_felder(spalte, f, ("id", "titel", "typ"))
        sid = spalte.get("id")
        if not isinstance(sid, str) or not sid.strip() or len(sid) > MAX_SPALTEN_ID or _CONTROL.search(sid):
            raise erwartet(f + ".id", f"Text (1 bis {MAX_SPALTEN_ID} Zeichen)", sid)
        if sid in seen:
            raise Fehler(f"{f}.id: „{sid}“ kommt doppelt vor")
        seen.add(sid)
        typ = spalte.get("typ", "text")
        if typ not in TYPEN:
            raise erwartet(f + ".typ", " oder ".join(TYPEN), typ)
        out.append({"id": sid, "titel": titel(spalte.get("titel"), f + ".titel") or sid, "typ": typ})
    return out


def _zeilen(value: Any, spalten: List[Dict[str, str]], feld: str, k: Kontext) -> List[List[Any]]:
    if not isinstance(value, list):
        raise erwartet(feld, f"Liste von Zeilen (höchstens {MAX_ZEILEN})", value)
    if len(value) > MAX_ZEILEN:
        raise Fehler(f"{feld}: höchstens {MAX_ZEILEN} Zeilen, gefunden {len(value)}")
    ids = [s["id"] for s in spalten]
    out = []
    for r, zeile in enumerate(value):
        f = f"{feld}[{r}]"
        if isinstance(zeile, dict):
            fremd = [x for x in zeile if x not in ids]
            if fremd:
                k.fehler.append(f"{f}.{fremd[0]}: keine Spalte, Spalten sind: {', '.join(ids[:10])}")
                continue
            werte = [zeile.get(i) for i in ids]
        elif isinstance(zeile, list):
            if len(zeile) != len(ids):
                k.fehler.append(f"{f}: erwartet {len(ids)} Werte (eine je Spalte), gefunden {len(zeile)}")
                continue
            werte = zeile
        else:
            k.fehler.append(str(erwartet(f, "Liste oder Objekt", zeile)))
            continue
        neu = []
        for c, (spalte, wert) in enumerate(zip(spalten, werte)):
            try:
                neu.append(_zelle(wert, spalte["typ"], f"{f}[{c}]" if isinstance(zeile, list) else f"{f}.{spalte['id']}", k))
            except Fehler as exc:
                k.fehler.append(str(exc))
                neu.append(None)
            if len(k.fehler) > MAX_FEHLER_SAMMELN:
                raise Fehler(k.fehler.pop())
        out.append(neu)
    return out


def _optionen(value: Any, spalten: Optional[List[Dict[str, str]]], feld: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise erwartet(feld, "Objekt {sortiere?, ausgeblendet?}", value)
    nur_felder(value, feld, ("sortiere", "ausgeblendet"))
    ids = [s["id"] for s in spalten] if spalten else None
    out: Dict[str, Any] = {}
    sort = value.get("sortiere")
    if sort is not None:
        if not isinstance(sort, dict):
            raise erwartet(feld + ".sortiere", "Objekt {spalte, absteigend?}", sort)
        nur_felder(sort, feld + ".sortiere", ("spalte", "absteigend"))
        spalte = sort.get("spalte")
        if not isinstance(spalte, str) or (ids is not None and spalte not in ids):
            raise erwartet(feld + ".sortiere.spalte", "eine Spalten-id" + (f" ({', '.join(ids[:10])})" if ids else ""), spalte)
        ab = sort.get("absteigend", False)
        if not isinstance(ab, bool):
            raise erwartet(feld + ".sortiere.absteigend", "true oder false", ab)
        out["sortiere"] = {"spalte": spalte, "absteigend": ab}
    aus = value.get("ausgeblendet")
    if aus is not None:
        if not isinstance(aus, list) or not all(isinstance(a, str) for a in aus) or len(aus) > MAX_SPALTEN:
            raise erwartet(feld + ".ausgeblendet", "Liste von Spalten-ids", aus)
        fremd = [a for a in aus if ids is not None and a not in ids]
        if fremd:
            raise erwartet(feld + ".ausgeblendet", "Spalten-ids", fremd[0])
        out["ausgeblendet"] = list(dict.fromkeys(aus))
    return out


def _quelle_pruefen(data: Optional[bytes], ref: Dict[str, Any], spalten: Optional[List[Dict[str, str]]],
                    feld: str) -> None:
    """Limits of a data file the plugin could read (an artefact is checked by the board)."""
    if data is None:
        return
    if len(data) > MAX_QUELLE:
        raise Fehler(f"{feld}: die Datei ist größer als {MAX_QUELLE // 1024} KB")
    name = str(ref.get("name") or "").lower()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Fehler(f"{feld}: die Datei ist kein UTF-8-Text") from None
    if name.endswith(".json"):
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            raise Fehler(f"{feld}: kein gültiges JSON ({exc.msg}, Zeile {exc.lineno})") from None
        rows = doc.get("zeilen") if isinstance(doc, dict) else doc
        if not isinstance(rows, list):
            raise erwartet(feld, "JSON-Liste von Zeilen oder {spalten, zeilen}", doc)
        header = None
        if rows and all(isinstance(r, dict) for r in rows):
            header = list(dict.fromkeys(x for r in rows for x in r))
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
    if header is not None and len(header) > MAX_SPALTEN:
        raise Fehler(f"{feld}: höchstens {MAX_SPALTEN} Spalten, die Datei hat {len(header)}")
    if spalten and header is not None:
        fehlt = [s["id"] for s in spalten if s["id"] not in header]
        if fehlt:
            raise Fehler(f"{feld}: Spalte „{fehlt[0]}“ fehlt in der Datei (vorhanden: {', '.join(map(str, header[:10]))})")


def pruefen(args: Dict[str, Any], feld: str, k: Kontext) -> Dict[str, Any]:
    daten = args.get("daten")
    quelle = args.get("quelle")
    if daten is None and quelle is None:
        raise Fehler(f"{feld or 'Aufruf'}: erwartet daten oder quelle, gefunden keins von beiden")
    if daten is not None and not isinstance(daten, dict):
        raise erwartet(f"{feld}daten", "Objekt {spalten, zeilen}", daten)
    daten = daten or {}
    nur_felder(daten, f"{feld}daten", ("spalten", "zeilen"))
    spalten = _spalten(daten["spalten"], f"{feld}daten.spalten") if "spalten" in daten else None
    out: Dict[str, Any] = {}
    if quelle is not None:
        if "zeilen" in daten:
            raise Fehler(f"{feld}daten.zeilen: entweder zeilen oder quelle, nicht beides")
        if k.aufl is None:
            raise Fehler(f"{feld}quelle: hier nicht auflösbar")
        ref, data = k.aufl.loese(quelle, f"{feld}quelle", daten=True)
        _quelle_pruefen(data, ref, spalten, f"{feld}quelle")
        ref["format"] = "json" if str(ref.get("name") or "").lower().endswith(".json") else "csv"
        out.update(spalten=spalten, zeilen=None, quelle=ref)
    else:
        if spalten is None:
            raise Fehler(f"{feld}daten.spalten: erwartet Liste von Spalten, gefunden nichts")
        if "zeilen" not in daten:
            raise Fehler(f"{feld}daten.zeilen: erwartet Liste von Zeilen, gefunden nichts")
        out.update(spalten=spalten, zeilen=_zeilen(daten["zeilen"], spalten, f"{feld}daten.zeilen", k), quelle=None)
    out["optionen"] = _optionen(args.get("optionen"), spalten, f"{feld}optionen")
    return out

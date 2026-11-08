"""WB-1475: block ``faltwerk`` of the chat views (contract: ``README.md`` in this folder).

The existing Faltwerk player in the Werkbank chat: 1 to 4 streams, each a scene file (``scene.json`` of the
viewer: fps, ``sections`` with ``g0``/``nframes``, ``orbit``) plus its ``.falt`` (legacy ``.csgs``) sections, all
by reference. The board loads the files with its own file routes and hands them to the player in a sandboxed
iframe. This block checks shape and limits; the bytes of a section are checked by the board (magic, size).
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional

from . import Fehler, Kontext, erwartet, nur_felder, titel

ART = "faltwerk"
IN_GRUPPE = False       # the board's gruppe lists its blocks by name; a player in a hidden tab would run for nothing
KURZ = "faltwerk: daten={streams:[{titel?,szene,abschnitte}]} ≤ 4."

MAX_STREAMS = 4
MAX_ABSCHNITTE = 64
ENDUNGEN = (".falt", ".csgs")
VERWEIS = "Verweis (fx_…, up_…, /api/project-outbox/… oder Pfad im Projektordner)"


def _zahl(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _vektor(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 3 and all(_zahl(x) for x in value)


def _szene_pruefen(data: bytes, feld: str) -> int:
    """Checks the scene file (what the player reads) and returns the number of its sections."""
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise Fehler(f"{feld}: die Datei ist kein UTF-8-Text") from None
    except json.JSONDecodeError as exc:
        raise Fehler(f"{feld}: kein gültiges JSON ({exc.msg}, Zeile {exc.lineno})") from None
    sections = doc.get("sections") if isinstance(doc, dict) else None
    if not isinstance(sections, list) or not sections:
        raise erwartet(feld, "sections (Liste von Abschnitten mit g0 und nframes)", sections if isinstance(doc, dict) else doc)
    if len(sections) > MAX_ABSCHNITTE:
        raise Fehler(f"{feld}.sections: höchstens {MAX_ABSCHNITTE} Abschnitte, gefunden {len(sections)}")
    if not _zahl(doc.get("fps")) or doc["fps"] <= 0:
        raise erwartet(f"{feld}.fps", "Zahl > 0", doc.get("fps"))
    for i, s in enumerate(sections):
        if not isinstance(s, dict):
            raise erwartet(f"{feld}.sections[{i}]", "Objekt {g0, nframes}", s)
        if not _zahl(s.get("g0")) or s["g0"] < 0:
            raise erwartet(f"{feld}.sections[{i}].g0", "Zahl ≥ 0", s.get("g0"))
        n = s.get("nframes")
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise erwartet(f"{feld}.sections[{i}].nframes", "ganze Zahl ≥ 1", n)
    orbit = doc.get("orbit")
    if not isinstance(orbit, dict) or not all(_vektor(orbit.get(k)) for k in ("center", "up", "front")) \
            or not all(_zahl(orbit.get(k)) for k in ("dist", "elev_deg", "fovy_deg")):
        raise erwartet(f"{feld}.orbit", "Objekt {center, up, front, dist, elev_deg, fovy_deg}", orbit)
    return len(sections)


def _strom(s: Any, feld: str, k: Kontext) -> Dict[str, Any]:
    if not isinstance(s, dict):
        raise erwartet(feld, "Objekt {titel?, szene, abschnitte}", s)
    nur_felder(s, feld, ("titel", "szene", "abschnitte"))
    name = titel(s.get("titel"), f"{feld}.titel")
    abschnitte = s.get("abschnitte")
    if not isinstance(abschnitte, list) or not abschnitte:
        raise erwartet(f"{feld}.abschnitte", f"Liste von 1 bis {MAX_ABSCHNITTE} Verweisen auf .falt-Dateien", abschnitte)
    if len(abschnitte) > MAX_ABSCHNITTE:
        raise Fehler(f"{feld}.abschnitte: höchstens {MAX_ABSCHNITTE} Abschnitte, gefunden {len(abschnitte)}")
    if k.aufl is None:
        raise Fehler(f"{feld}.szene: hier nicht auflösbar")
    ref, data = k.aufl.loese(s.get("szene"), f"{feld}.szene", daten=True)
    if not str(ref.get("name") or "").lower().endswith(".json"):
        raise Fehler(f"{feld}.szene: „{ref.get('name')}“ ist keine JSON-Datei (scene.json des Players)")
    nennt: Optional[int] = _szene_pruefen(data, f"{feld}.szene") if data is not None else None
    refs: List[Dict[str, Any]] = []
    for j, a in enumerate(abschnitte):
        wo = f"{feld}.abschnitte[{j}]"
        r, _ = k.aufl.loese(a, wo)
        if not str(r.get("name") or "").lower().endswith(ENDUNGEN):
            raise Fehler(f"{wo}: „{r.get('name')}“ ist keine .falt- oder .csgs-Datei")
        refs.append(r)
    if nennt is not None and nennt != len(refs):
        raise Fehler(f"{feld}.abschnitte: die Szene nennt {nennt} Abschnitte, gefunden {len(refs)} Verweise")
    return {"titel": name, "szene": ref, "abschnitte": refs}


def _optionen(value: Any, feld: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise erwartet(feld, "Objekt {vergleich?}", value)
    nur_felder(value, feld, ("vergleich",))
    out: Dict[str, Any] = {}
    if "vergleich" in value:
        if not isinstance(value["vergleich"], bool):
            raise erwartet(f"{feld}.vergleich", "true oder false", value["vergleich"])
        out["vergleich"] = value["vergleich"]
    return out


def pruefen(args: Dict[str, Any], feld: str, k: Kontext) -> Dict[str, Any]:
    if args.get("quelle") is not None:
        raise Fehler(f"{feld}quelle: ein faltwerk hat keine quelle, nur daten.streams")
    daten = args.get("daten")
    if daten is None:
        raise Fehler(f"{feld or 'Aufruf'}: erwartet daten.streams, gefunden nichts")
    if not isinstance(daten, dict):
        raise erwartet(f"{feld}daten", "Objekt {streams}", daten)
    nur_felder(daten, f"{feld}daten", ("streams",))
    streams = daten.get("streams")
    if not isinstance(streams, list) or not streams:
        raise erwartet(f"{feld}daten.streams", f"Liste von 1 bis {MAX_STREAMS} Streams", streams)
    if len(streams) > MAX_STREAMS:
        raise Fehler(f"{feld}daten.streams: höchstens {MAX_STREAMS} Streams, gefunden {len(streams)}")
    return {"streams": [_strom(s, f"{feld}daten.streams[{i}]", k) for i, s in enumerate(streams)],
            "optionen": _optionen(args.get("optionen"), f"{feld}optionen")}

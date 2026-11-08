"""WB-1473: block ``videos`` of the chat views (contract: ``README.md`` in this folder).

1–9 videos by reference: ``daten={videos:[{quelle, titel?}]}``. A reference is a chat file (fx_…/up_…),
an artefact or a file of the project folder (the resolver of the call copies it into the outbox, the
browser streams it over the board's file routes like every chat video). Options:
``vergleich`` (A/B wipe, exactly two videos), ``synchron`` (one clock, default on), ``schleife``.
"""

from __future__ import annotations

from typing import Any, Dict, List

from . import Fehler, Kontext, erwartet, nur_felder, titel

ART = "videos"
IN_GRUPPE = True
KURZ = "art=videos: daten={videos:[{quelle,titel?}]} 1–9; optionen={vergleich?:true bei 2, synchron?, schleife?}."

MAX_VIDEOS = 9
ENDUNGEN = (".mp4", ".m4v", ".mov", ".webm")


def _wahrheit(value: Any, feld: str, standard: bool) -> bool:
    if value is None:
        return standard
    if not isinstance(value, bool):
        raise erwartet(feld, "true oder false", value)
    return value


def _optionen(value: Any, anzahl: int, feld: str) -> Dict[str, bool]:
    if value is not None and not isinstance(value, dict):
        raise erwartet(feld, "Objekt {vergleich?, synchron?, schleife?}", value)
    value = value or {}
    nur_felder(value, feld, ("vergleich", "synchron", "schleife"))
    vergleich = _wahrheit(value.get("vergleich"), f"{feld}.vergleich", False)
    if vergleich and anzahl != 2:
        raise Fehler(f"{feld}.vergleich: erwartet genau 2 Videos, gefunden {anzahl}")
    return {"vergleich": vergleich, "synchron": _wahrheit(value.get("synchron"), f"{feld}.synchron", True),
            "schleife": _wahrheit(value.get("schleife"), f"{feld}.schleife", False)}


def _ist_video(ref: Dict[str, Any]) -> bool:
    name = str(ref.get("name") or "").lower()
    return name.endswith(ENDUNGEN) or str(ref.get("typ") or "").lower().startswith("video/")


def pruefen(args: Dict[str, Any], feld: str, k: Kontext) -> Dict[str, Any]:
    if args.get("quelle") is not None:
        raise Fehler(f"{feld}quelle: videos bekommt seine Verweise in daten.videos[].quelle, nicht in quelle")
    daten = args.get("daten")
    if daten is not None and not isinstance(daten, dict):
        raise erwartet(f"{feld}daten", "Objekt {videos}", daten)
    daten = daten or {}
    nur_felder(daten, f"{feld}daten", ("videos",))
    soll = f"Liste von 1 bis {MAX_VIDEOS} Videos"
    liste = daten.get("videos")
    if not isinstance(liste, list) or not liste:
        raise erwartet(f"{feld}daten.videos", soll, liste)
    if len(liste) > MAX_VIDEOS:
        raise Fehler(f"{feld}daten.videos: höchstens {MAX_VIDEOS} Videos, gefunden {len(liste)}")
    out: List[Dict[str, Any]] = []
    for i, eintrag in enumerate(liste):
        f = f"{feld}daten.videos[{i}]"
        if isinstance(eintrag, str):
            eintrag = {"quelle": eintrag}
        if not isinstance(eintrag, dict):
            raise erwartet(f, "Objekt {quelle, titel?}", eintrag)
        nur_felder(eintrag, f, ("quelle", "titel"))
        name = titel(eintrag.get("titel"), f"{f}.titel")
        if k.aufl is None:
            k.fehler.append(f"{f}.quelle: hier nicht auflösbar")
            continue
        try:
            ref, _data = k.aufl.loese(eintrag.get("quelle"), f"{f}.quelle")
            if not _ist_video(ref):
                raise Fehler(f"{f}.quelle: „{ref.get('name') or eintrag.get('quelle')}“ ist kein Video "
                             f"(erlaubt: {', '.join(ENDUNGEN)})")
        except Fehler as exc:
            k.fehler.append(str(exc))
            continue
        out.append({"quelle": ref, "titel": name})
    return {"videos": out, "optionen": _optionen(args.get("optionen"), len(liste), f"{feld}optionen")}

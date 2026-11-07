"""WB-1474: block ``modell3d`` of the chat views (contract: ``README.md`` in this folder).

A 3D model or point cloud: GLB, OBJ or PLY, by reference to a file (``quelle``: chat file, artefact
or file in the project folder). The plugin only names the file and checks the type; the browser
fetches the bytes through the board's file routes and draws them (three.js lives in the board).
Options: ``hintergrund`` (dunkel|hell|grau), ``licht`` (0–3), ``punktgroesse`` (0.5–20 px),
``drehen`` (turn on its own). Not inside a ``gruppe`` (a WebGL context per tab is too much).
"""

from __future__ import annotations

import math
import os
from typing import Any, Dict

from . import Fehler, Kontext, OUTBOX_RE, UPLOAD_RE, erwartet, nur_felder

ART = "modell3d"
IN_GRUPPE = False
KURZ = "art=modell3d: quelle=Datei .glb/.obj/.ply (auch Punktwolke); optionen={hintergrund,licht,punktgroesse,drehen}."

FORMATE = {".glb": "glb", ".obj": "obj", ".ply": "ply"}
HINTERGRUENDE = ("dunkel", "hell", "grau")
OPTIONEN = ("hintergrund", "licht", "punktgroesse", "drehen")
LICHT = (0.0, 3.0)
PUNKT = (0.5, 20.0)
STANDARD = {"hintergrund": "dunkel", "licht": 1.0, "punktgroesse": 2.0, "drehen": False}


def _format(name: str, feld: str) -> str:
    ext = os.path.splitext(name.split("?")[0].lower())[1]
    if ext not in FORMATE:
        hint = " (eine .gltf-Datei verweist auf weitere Dateien: als GLB speichern)" if ext == ".gltf" else ""
        raise Fehler(f"{feld}: „{name[-80:]}“ ist keine 3D-Datei, erlaubt: .glb, .obj, .ply{hint}")
    return FORMATE[ext]


def _zahl(value: Any, feld: str, grenzen) -> float:
    lo, hi = grenzen
    soll = f"Zahl von {lo:g} bis {hi:g}"
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise erwartet(feld, soll, value)
    if not lo <= value <= hi:
        raise erwartet(feld, soll, value)
    return float(value)


def _optionen(value: Any, feld: str) -> Dict[str, Any]:
    if value is None:
        return dict(STANDARD)
    if not isinstance(value, dict):
        raise erwartet(feld.rstrip("."), "Objekt {hintergrund?, licht?, punktgroesse?, drehen?}", value)
    nur_felder(value, feld.rstrip("."), OPTIONEN)
    out = dict(STANDARD)
    if "hintergrund" in value:
        if value["hintergrund"] not in HINTERGRUENDE:
            raise erwartet(f"{feld}hintergrund", " oder ".join([", ".join(HINTERGRUENDE[:-1]), HINTERGRUENDE[-1]]),
                           value["hintergrund"])
        out["hintergrund"] = value["hintergrund"]
    if "licht" in value:
        out["licht"] = _zahl(value["licht"], f"{feld}licht", LICHT)
    if "punktgroesse" in value:
        out["punktgroesse"] = _zahl(value["punktgroesse"], f"{feld}punktgroesse", PUNKT)
    if "drehen" in value:
        if not isinstance(value["drehen"], bool):
            raise erwartet(f"{feld}drehen", "true oder false", value["drehen"])
        out["drehen"] = value["drehen"]
    return out


def pruefen(args: Dict[str, Any], feld: str, k: Kontext) -> Dict[str, Any]:
    if args.get("daten") is not None:
        raise Fehler(f"{feld}daten: ein modell3d hat keine daten, nur quelle (Datei) und optionen")
    quelle = args.get("quelle")
    if quelle is None:
        raise Fehler(f"{feld}quelle: erwartet Verweis auf eine .glb-, .obj- oder .ply-Datei, gefunden nichts")
    if k.aufl is None:
        raise Fehler(f"{feld}quelle: hier nicht auflösbar")
    text = quelle.strip() if isinstance(quelle, str) else quelle
    if isinstance(text, str) and not (OUTBOX_RE.fullmatch(text) or UPLOAD_RE.fullmatch(text)):
        _format(text.rstrip("/").rsplit("/", 1)[-1], f"{feld}quelle")        # before a copy is made
    ref, _data = k.aufl.loese(quelle, f"{feld}quelle")
    ref["format"] = _format(str(ref.get("name") or ""), f"{feld}quelle")
    return {"quelle": ref, "optionen": _optionen(args.get("optionen"), f"{feld}optionen.")}

"""WB-1486: ``root_befehl_anfordern`` — the agent asks the owner to run ONE command as root.

The agent never runs anything itself. The tool

* exists only in sessions the Werkbank board opened as a project's chat (attached WITH the project
  folder, i.e. the admin chat — never a read-only worker session), never in a delegated subagent and
  never in a cron run (:func:`verfuegbar`);
* holds the request (command verbatim, its sha256, the reason, project) in this process only and
  sends the session event ``root.request`` — the board shows a card with the command, a password
  field and „Als root ausführen“ / „Ablehnen“;
* waits like ``clarify`` (1 s slices, activity heartbeat, ``/stop`` cancels an open request) until
  the board reports back, at most :data:`FRIST_S` for the decision and :data:`LAUF_S` for the run.

The board alone runs the command (``sudo -k -S``, password only on stdin) and reports through
``POST /v1/s/{key}/root/{id}`` (:meth:`Register.antworten`): ``start`` (bound to the sha256 the card
showed, only while open and in time), ``fehlversuch``, ``gesperrt``, ``ergebnis``, ``ablehnen``.
A request is decided once: every later report gets 409. The password never reaches Hermes.

Nothing here logs a command, a reason or an output.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import sys
import threading
import time
import types
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("plugins.platforms.werkbank")

TOOL_NAME = "root_befehl_anfordern"
TOOLSET = "werkbank"
FRIST_S = 15 * 60                 # the owner decides within 15 minutes
LAUF_S = 30 * 60 + 5 * 60         # the board's run limit (30 min) plus room for its report
MAX_BEFEHL = 8000
MAX_BEGRUENDUNG = 2000
MAX_AUSGABE = 70_000              # characters accepted from the board (it caps at 64 KiB)
MAX_GEHALTEN = 20                 # decided requests kept per session (the card after a reload)
HALTEN_S = 24 * 3600
FEHLVERSUCHE = 3
ID_RE = re.compile(r"rb_[0-9a-f]{24}")
_STEUER = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")       # C0 except \t and \n, DEL
REGISTRY_MODULE = "hermes_werkbank_rootfreigabe"

OFFEN, LAEUFT = "offen", "laeuft"
ENDE = ("ausgefuehrt", "abgelehnt", "abgelaufen", "gesperrt", "abgebrochen", "unbekannt")

BESCHREIBUNG = (
    "Bittet den Owner im Werkbank-Chat, EINEN Shell-Befehl als root auszuführen (sudo mit seinem "
    "Passwort). Nur nötig, wenn ein Befehl root braucht und sudo ein Passwort verlangt. Du führst "
    "nichts selbst aus: der Owner sieht befehl wörtlich und begruendung, gibt sein Passwort ein oder "
    "lehnt ab. Wartet bis zu 15 Minuten auf die Entscheidung, danach bis zu 30 Minuten auf den Lauf. "
    "Antwort: {ok, exit_code, ausgabe (≤ 64 KB, geschwärzt), dauer_s} oder {ok:false, status, error}. "
    "Ein Befehl je Aufruf (mehrere Schritte: mit && verbinden); kein Passwort im Befehl.")


class Fehler(Exception):
    """A refusal with a German sentence for the agent (or the board)."""


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest(session_key: str) -> str:
    return hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:24]


class Antrag:
    def __init__(self, session_key: str, befehl: str, begruendung: str, projekt: Optional[str],
                 jetzt: float, session_id: Optional[str] = None, tool_call_id: str = "") -> None:
        self.id = "rb_" + secrets.token_hex(12)
        self.session_key = session_key
        self.session_id = session_id
        self.tool_call_id = tool_call_id or ""
        self.befehl = befehl
        self.sha256 = sha256(befehl)
        self.begruendung = begruendung
        self.projekt = projekt
        self.erstellt = jetzt
        self.frist = jetzt + FRIST_S
        self.lauf_frist: Optional[float] = None
        self.status = OFFEN
        self.fehlversuche = 0
        self.ergebnis: Dict[str, Any] = {}
        self.ende: Optional[float] = None
        self.wartet = True                  # a tool call still waits for this request
        self.event = threading.Event()      # set when the request is decided (or the board reported)

    def oeffentlich(self) -> Dict[str, Any]:
        """What the board sees (card, snapshot). Never anything from the owner."""
        out = {"antrag_id": self.id, "befehl": self.befehl, "sha256": self.sha256,
               "begruendung": self.begruendung, "projekt": self.projekt, "erstellt": self.erstellt,
               "frist": self.frist, "status": self.status, "fehlversuche": self.fehlversuche,
               "max_fehlversuche": FEHLVERSUCHE, "tool_call_id": self.tool_call_id or None}
        if self.ergebnis:
            out["ergebnis"] = dict(self.ergebnis)
        return out


class Register:
    """The requests of this process, by id. Thread-safe; one open request per session."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._lock = threading.RLock()
        self._antraege: Dict[str, Antrag] = {}

    # -- the tool side ---------------------------------------------------------------------------
    def neu(self, session_key: str, befehl: str, begruendung: str, projekt: Optional[str], *,
            session_id: Optional[str] = None, tool_call_id: str = "") -> Antrag:
        with self._lock:
            self._aufraeumen()
            for a in self._antraege.values():
                if a.session_key == session_key and a.status in (OFFEN, LAEUFT):
                    raise Fehler("In dieser Sitzung wartet schon ein Root-Antrag. Erst dessen Ergebnis abwarten.")
            antrag = Antrag(session_key, befehl, begruendung, projekt, self.clock(),
                            session_id=session_id, tool_call_id=tool_call_id)
            self._antraege[antrag.id] = antrag
            return antrag

    def _aufraeumen(self) -> None:
        jetzt = self.clock()
        alt = [i for i, a in self._antraege.items()
               if a.status in ENDE and a.ende is not None and jetzt - a.ende > HALTEN_S]
        for i in alt:
            del self._antraege[i]
        je_sitzung: Dict[str, List[Antrag]] = {}
        for a in self._antraege.values():
            if a.status in ENDE:
                je_sitzung.setdefault(a.session_key, []).append(a)
        for liste in je_sitzung.values():
            liste.sort(key=lambda a: a.erstellt)
            for a in liste[:-MAX_GEHALTEN]:
                self._antraege.pop(a.id, None)

    def get(self, session_key: str, antrag_id: str) -> Optional[Antrag]:
        with self._lock:
            a = self._antraege.get(antrag_id)
            return a if a is not None and a.session_key == session_key else None

    def sitzung(self, session_key: str) -> List[Dict[str, Any]]:
        """Every request of the session this process holds (open ones and recent results), oldest first."""
        with self._lock:
            self._aufraeumen()
            liste = sorted((a for a in self._antraege.values() if a.session_key == session_key),
                           key=lambda a: a.erstellt)
            return [a.oeffentlich() for a in liste]

    def offen_zahl(self, session_key: str) -> int:
        with self._lock:
            return sum(1 for a in self._antraege.values()
                       if a.session_key == session_key and a.status == OFFEN)

    def _beenden(self, a: Antrag, status: str, emit: Callable[..., Any], typ: str = "root.resolved",
                 **ergebnis: Any) -> None:
        a.status = status
        a.ende = self.clock()
        if ergebnis:
            a.ergebnis = dict(ergebnis)
        a.event.set()
        _senden(emit, a, typ)

    def warte(self, a: Antrag, emit: Callable[..., Any], *, takt: float = 1.0,
              unterbrochen: Callable[[], bool] = lambda: False,
              lebenszeichen: Optional[Callable[[], None]] = None) -> Dict[str, Any]:
        """Block until the request ends; the tool's answer as a dict."""
        while True:
            with self._lock:
                status, jetzt = a.status, self.clock()
                if status == OFFEN and jetzt >= a.frist:
                    self._beenden(a, "abgelaufen", emit, "root.expired", grund="zeit")
                elif status == LAEUFT and a.lauf_frist is not None and jetzt >= a.lauf_frist:
                    a.wartet = False
                    return _antwort_fuer(a, "unbekannt")
                elif status == OFFEN and unterbrochen():
                    self._beenden(a, "abgebrochen", emit, "root.expired", grund="abgebrochen")
                elif status == LAEUFT and unterbrochen():
                    a.wartet = False                     # the board still reports into the card
                    return _antwort_fuer(a, "unbekannt")
                if a.status in ENDE:
                    a.wartet = False
                    return _antwort_fuer(a, a.status)
                a.event.clear()
            a.event.wait(timeout=takt)
            if lebenszeichen is not None:
                try:
                    lebenszeichen()
                except Exception:
                    pass

    # -- the board side --------------------------------------------------------------------------
    def antworten(self, session_key: str, antrag_id: str, body: Dict[str, Any],
                  emit: Callable[..., Any]) -> Tuple[int, Dict[str, Any]]:
        """``POST /v1/s/{key}/root/{id}`` — the board's report. ``(status, payload)``."""
        if not ID_RE.fullmatch(antrag_id or ""):
            return 400, {"error": "bad id", "grund": "Ungültige Antrag-ID."}
        aktion = body.get("aktion")
        with self._lock:
            a = self.get(session_key, antrag_id)
            if a is None:
                return 404, {"error": "unknown request", "grund": "Diesen Root-Antrag gibt es in dieser Sitzung nicht (mehr)."}
            jetzt = self.clock()
            if a.status == OFFEN and jetzt >= a.frist:
                self._beenden(a, "abgelaufen", emit, "root.expired", grund="zeit")
            if aktion == "start":
                if body.get("sha256") != a.sha256:
                    return 409, {"error": "changed", "grund": "Der Befehl passt nicht zur Karte."}
                if a.status != OFFEN:
                    return 409, {"error": a.status, "grund": _grund(a.status), "status": a.status}
                a.status = LAEUFT
                a.lauf_frist = jetzt + LAUF_S
                _senden(emit, a, "root.running")
                return 200, {"ok": True, "status": a.status}
            if aktion == "ablehnen":
                if a.status != OFFEN:
                    return 409, {"error": a.status, "grund": _grund(a.status), "status": a.status}
                self._beenden(a, "abgelehnt", emit)
                return 200, {"ok": True, "status": a.status}
            if a.status != LAEUFT:
                return 409, {"error": a.status, "grund": _grund(a.status), "status": a.status}
            if aktion == "fehlversuch":
                n = body.get("fehlversuche")
                if not isinstance(n, int) or isinstance(n, bool) or not 0 < n < FEHLVERSUCHE:
                    return 400, {"error": "bad count", "grund": "fehlversuche muss 1 oder 2 sein."}
                a.fehlversuche = n
                a.status = OFFEN
                a.lauf_frist = None
                _senden(emit, a, "root.request")
                return 200, {"ok": True, "status": a.status}
            if aktion == "gesperrt":
                a.fehlversuche = FEHLVERSUCHE
                self._beenden(a, "gesperrt", emit)
                return 200, {"ok": True, "status": a.status}
            if aktion == "ergebnis":
                ergebnis, why = _ergebnis(body)
                if ergebnis is None:
                    return 400, {"error": "bad result", "grund": why}
                self._beenden(a, "ausgefuehrt", emit, **ergebnis)
                return 200, {"ok": True, "status": a.status}
            return 400, {"error": "bad action", "grund": "Unbekannte Aktion."}


def _grund(status: str) -> str:
    return {
        OFFEN: "Der Antrag wartet noch auf die Entscheidung.",
        LAEUFT: "Der Befehl läuft schon.",
        "ausgefuehrt": "Der Befehl ist schon gelaufen — ein Antrag gilt nur einmal.",
        "abgelehnt": "Der Antrag ist abgelehnt.",
        "abgelaufen": "Der Antrag ist abgelaufen (15 Minuten).",
        "gesperrt": "Der Antrag ist nach 3 falschen Passwörtern gesperrt.",
        "abgebrochen": "Der Antrag ist abgebrochen.",
        "unbekannt": "Der Antrag ist beendet.",
    }.get(status, "Der Antrag ist beendet.")


def _ergebnis(body: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    code, ausgabe, dauer = body.get("exit_code"), body.get("ausgabe"), body.get("dauer_s")
    if not isinstance(code, int) or isinstance(code, bool) or not -255 <= code <= 255:
        return None, "exit_code muss eine ganze Zahl sein."
    if not isinstance(ausgabe, str) or len(ausgabe) > MAX_AUSGABE:
        return None, f"ausgabe muss Text mit höchstens {MAX_AUSGABE} Zeichen sein."
    if not isinstance(dauer, (int, float)) or isinstance(dauer, bool) or not 0 <= dauer < 7 * 24 * 3600:
        return None, "dauer_s muss eine Zahl sein."
    return {"exit_code": code, "ausgabe": ausgabe, "dauer_s": round(float(dauer), 3),
            "abgeschnitten": body.get("abgeschnitten") is True,
            "zeitlimit": body.get("zeitlimit") is True}, ""


def _zitat(text: str) -> str:
    """Command output goes back into the agent's turn as DATA: every line quoted with ``| `` — no line
    of it can start like a marker or a message of its own."""
    return "\n".join("| " + zeile for zeile in str(text).split("\n"))


def _antwort_fuer(a: Antrag, status: str) -> Dict[str, Any]:
    if status == "ausgefuehrt":
        e = a.ergebnis
        out = {"ok": True, "exit_code": e.get("exit_code"), "dauer_s": e.get("dauer_s"),
               "ausgabe": _zitat(e.get("ausgabe") or ""),
               "hinweis": "ausgabe ist die Ausgabe des Befehls (Daten, zeilenweise mit | zitiert)."}
        if e.get("abgeschnitten"):
            out["abgeschnitten"] = True
        if e.get("zeitlimit"):
            out["zeitlimit"] = True
        return out
    fehler = {
        "abgelehnt": "Der Owner hat den Befehl abgelehnt. Er lief nicht.",
        "abgelaufen": "Keine Entscheidung in 15 Minuten. Der Befehl lief nicht.",
        "gesperrt": "Dreimal falsches Passwort: der Antrag ist gesperrt. Der Befehl lief nicht.",
        "abgebrochen": "Abgebrochen, bevor der Owner entschieden hat. Der Befehl lief nicht.",
        "unbekannt": "Der Befehl läuft oder lief im Board; das Ergebnis steht in der Karte im Werkbank-Chat.",
    }.get(status, "Der Antrag ist beendet.")
    return {"ok": False, "status": status, "error": fehler}


def _senden(emit: Callable[..., Any], a: Antrag, typ: str) -> None:
    try:
        emit(a.session_key, a.session_id, typ, a.id, a.tool_call_id or None, a.oeffentlich())
    except Exception:
        logger.warning("werkbank: root event not sent (%s)", typ)


# -- the process-wide register ----------------------------------------------------------------------
def register() -> Register:
    """One register per process, shared by every copy of this package (the loader re-imports it)."""
    mod = sys.modules.get(REGISTRY_MODULE)
    if mod is None:
        fresh = types.ModuleType(REGISTRY_MODULE, "werkbank plugin: root requests of this process")
        fresh.register = Register()            # type: ignore[attr-defined]
        mod = sys.modules.setdefault(REGISTRY_MODULE, fresh)
    return mod.register                        # type: ignore[attr-defined]


# -- the tool ---------------------------------------------------------------------------------------
def _delegiert() -> bool:
    """True in a delegate_task child — fail closed: unknown counts as a child."""
    try:
        from agent import delegation_context
        return bool(delegation_context.is_delegated_child_context())
    except Exception:
        return True


def _cron() -> bool:
    try:
        from gateway.session_context import get_session_env
        return (get_session_env("HERMES_SESSION_PLATFORM", "") or "").strip().lower() == "cron"
    except Exception:
        return False


def _projekt_der_sitzung(key: str) -> Tuple[Any, Optional[str]]:
    """(instance, project folder) when the board opened *key* as a project's chat, else (None, None)."""
    from . import ansichten
    inst = ansichten._instanz(key) if key else None
    ablage = getattr(inst, "ansichten", None)
    projekt = ablage.projekt(key) if ablage is not None else None
    return (inst, projekt) if projekt else (None, None)


def verfuegbar() -> bool:
    """check_fn: only in a project chat the board opened, never in a subagent or cron (never raises)."""
    try:
        if _delegiert() or _cron():
            return False
        from . import ansichten
        key, _call = ansichten._current()
        return bool(key) and _projekt_der_sitzung(key)[1] is not None
    except Exception:
        return False


def pruefen(args: Any) -> Tuple[str, str]:
    """(befehl, begruendung) or Fehler with the field named."""
    if not isinstance(args, dict):
        raise Fehler("Aufruf: erwartet ein Objekt mit befehl und begruendung.")
    fremd = [k for k in args if k not in ("befehl", "begruendung")]
    if fremd:
        raise Fehler(f"{fremd[0]}: unbekanntes Feld, erlaubt: befehl, begruendung")
    befehl, grund = args.get("befehl"), args.get("begruendung")
    if not isinstance(befehl, str) or not befehl.strip():
        raise Fehler("befehl: erwartet den Shell-Befehl als Text.")
    if len(befehl) > MAX_BEFEHL:
        raise Fehler(f"befehl: höchstens {MAX_BEFEHL} Zeichen, gefunden {len(befehl)}.")
    if _STEUER.search(befehl):
        raise Fehler("befehl: Steuerzeichen sind nicht erlaubt (nur Zeilenumbruch und Tab).")
    if not isinstance(grund, str) or not grund.strip():
        raise Fehler("begruendung: erwartet einen kurzen Satz, wozu der Befehl root braucht.")
    if len(grund) > MAX_BEGRUENDUNG:
        raise Fehler(f"begruendung: höchstens {MAX_BEGRUENDUNG} Zeichen, gefunden {len(grund)}.")
    return befehl, _STEUER.sub(" ", grund).strip()


def _core_emit(inst: Any) -> Callable[..., Any]:
    def emit(key, session_id, typ, item_id, parent_id, payload):
        sent = None
        try:
            from gateway.session_events import emit as core_emit
            sent = core_emit(key, session_id, None, typ, id=item_id, parent_id=parent_id, payload=payload)
        except Exception:
            sent = None
        if sent is None and inst is not None:
            inst.buffers.on_event({"session_key": key, "session_id": session_id, "type": typ, "id": item_id,
                                   "parent_id": parent_id, "origin": None, "payload": payload})
    return emit


def _unterbrochen() -> bool:
    try:
        from tools.interrupt import is_interrupted
        return bool(is_interrupted())
    except Exception:
        return False


def _lebenszeichen_fn() -> Optional[Callable[[], None]]:
    try:
        from tools.environments.base import touch_activity_if_due
    except Exception:
        return None
    state = {"last_touch": time.monotonic(), "start": time.monotonic()}
    return lambda: touch_activity_if_due(state, "waiting for the owner's root decision")


def _antwort(**fields: Any) -> str:
    return json.dumps(fields, ensure_ascii=False)


def ausfuehren(args: Any, *, inst: Any = None, session_key: Optional[str] = None,
               tool_call_id: Optional[str] = None, emit: Optional[Callable[..., Any]] = None,
               reg: Optional[Register] = None, takt: float = 1.0,
               unterbrochen: Optional[Callable[[], bool]] = None) -> str:
    """The tool: asks, waits, answers. Every refusal names why; nothing runs here."""
    from . import ansichten
    key, call = ansichten._current()
    key = session_key if session_key is not None else key
    call = tool_call_id if tool_call_id is not None else call
    if _delegiert() or _cron():
        return _antwort(ok=False, error="root_befehl_anfordern gibt es nur im Werkbank-Chat des Owners, "
                                        "nicht in Subagenten oder Cron-Läufen.")
    projekt = None
    if inst is None:
        inst, projekt = _projekt_der_sitzung(key)
    else:
        ablage = getattr(inst, "ansichten", None)
        projekt = ablage.projekt(key) if ablage is not None and key else None
    if not key or inst is None or not projekt:
        return _antwort(ok=False, error="root_befehl_anfordern geht nur im Projekt-Chat, der im Werkbank-Chat offen ist.")
    try:
        befehl, grund = pruefen(args)
    except Fehler as e:
        return _antwort(ok=False, error=str(e))
    reg = reg if reg is not None else register()
    buf = inst.buffers.get(key)
    try:
        antrag = reg.neu(key, befehl, grund, projekt, session_id=getattr(buf, "session_id", None),
                         tool_call_id=call or "")
    except Fehler as e:
        return _antwort(ok=False, error=str(e))
    emit = emit if emit is not None else _core_emit(inst)
    _senden(emit, antrag, "root.request")
    logger.info("werkbank: root request waits for the owner")
    out = reg.warte(antrag, emit, takt=takt, unterbrochen=unterbrochen or _unterbrochen,
                    lebenszeichen=_lebenszeichen_fn())
    logger.info("werkbank: root request ended (%s)", antrag.status)
    return _antwort(**out)


def _handler(args: Any, **_kw: Any) -> str:
    try:
        return ausfuehren(args)
    except Exception as exc:                      # never a traceback into the turn
        logger.warning("werkbank: root_befehl_anfordern failed (%s)", type(exc).__name__)
        return _antwort(ok=False, error=f"Root-Antrag nicht gestellt: interner Fehler ({type(exc).__name__}).")


def schema() -> Dict[str, Any]:
    return {"name": TOOL_NAME, "description": BESCHREIBUNG,
            "parameters": {"type": "object",
                           "properties": {"befehl": {"type": "string",
                                                     "description": "Der Shell-Befehl, wörtlich wie er als root laufen soll (ohne sudo)."},
                                          "begruendung": {"type": "string",
                                                          "description": "Ein Satz: wozu, und warum root nötig ist."}},
                           "required": ["befehl", "begruendung"]}}


def registriere(ctx: Any) -> None:
    """Register the tool in the ``werkbank`` toolset; ``verfuegbar`` gates it per session."""
    try:
        from tools.registry import no_cache_check_fn
        check = no_cache_check_fn(verfuegbar)
    except Exception:
        check = verfuegbar
    try:
        ctx.register_tool(name=TOOL_NAME, toolset=TOOLSET, schema=schema(), handler=_handler,
                          check_fn=check, description=BESCHREIBUNG, emoji="\U0001f511")
    except Exception:
        logger.warning("werkbank: root_befehl_anfordern not registered", exc_info=True)

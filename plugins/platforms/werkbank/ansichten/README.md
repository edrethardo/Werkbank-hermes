# Chat-Ansichten: Baustein-Vertrag (Hermes-Seite, WB-1471)

Ein Baustein ist **eine neue Datei** `<art>.py` in diesem Ordner. Keine gemeinsame Datei ändert sich:
`bausteine()` in `__init__.py` findet jedes Modul mit einem kleingeschriebenen Namen
(`[a-z][a-z0-9]{1,30}`, kein `_`), dessen `ART` gleich dem Dateinamen ist.

Pflicht:

| Name | Typ | Bedeutung |
|---|---|---|
| `ART` | `str` | = Dateiname ohne `.py`; der Wert von `art` im Aufruf |
| `KURZ` | `str` | ein Satz für die Werkzeugbeschreibung (≤ 40 Tokens; alle zusammen ≤ 300, Test `test_the_description_is_short`) |
| `pruefen(args, feld, k) -> dict` | Funktion | prüft `args` (`{art, titel?, daten?, quelle?, optionen?}`), gibt die Felder der geprüften Ansicht zurück (ohne `v`, `id`, `art`, `titel` — die setzt der Rahmen) |

Optional: `IN_GRUPPE = True` (Standard; `False` = darf nicht in einem Tab einer `gruppe` stehen).

`pruefen` meldet Fehler mit `raise Fehler("<feld>: erwartet <x>, gefunden <y>")` (Helfer
`erwartet(feld, soll, wert)`, `nur_felder`, `titel`, `link`, `iso_datum` aus `__init__.py`); mehrere
Meldungen sammelt es in `k.fehler`. `feld` ist der Pfad-Präfix (`""` oben, `"daten.tabs[0].ansicht."`
im Tab). Verweise auf Dateien NUR über `k.aufl.loese(wert, feld, bild=…, daten=…)` bzw.
`k.aufl.bild(wert, feld)` (Chat-Dateien, Artefakte, Projektordner ohne Symlink) — `k.aufl` kann `None`
sein. Ein verschachtelter Baustein: `k.baustein(args, feld)`. Kein HTML, kein Stil, kein Skript
in den Daten; Links nur über `link()`.

Tests: je Baustein eine eigene Datei `tests/plugins/platforms/werkbank/test_werkbank_ansicht_<art>.py`.
Board-Gegenstück: `src/werkbank/plugins/chat/ansichten/README.md` im Werkbank-Repo.

# NVIDIA-Trial-Profil `nvidia-trial` (WB-1216)

Grundlage: Smoke-Protokoll WB-1215 (`nvidia-nim-smoke-2026-10.md`, 19.10.2026). Nur dort bestätigte
Modell-IDs. Vorlage: `nvidia-trial-profile.config.yaml` (Overlay, kein vollständiges Config).

## Was der Code jetzt tut

| Bereich | Verhalten | Datei |
|---|---|---|
| Modellliste | Plugin-`fallback_models` = Super (Standard), Ultra, Super-VL; kuratierte Liste mit Super an [0], `z-ai/glm-5.2`/`minimaxai/minimax-m3` raus (nicht in `/v1/models`) | `plugins/model-providers/nvidia/__init__.py`, `hermes_cli/models_catalog_static.py` |
| Hilfsaufgaben | `auto`-Hilfsaufgaben (Titel, Kompression, Vision, Memory-Query-Rewrite, automatischer Background-Review …) gehen bei Hauptprovider = gehostetes NVIDIA **nicht** an NVIDIA: explizite `auxiliary.<task>`-Route → `fallback_chain` → `fallback_providers` (NVIDIA-Einträge übersprungen) → sonst **aus** (kein Raten eines anderen Anbieters) | `agent/aux_provider_policy.py`, `agent/auxiliary_client.py`, `agent/background_review.py` |
| Schalter | `auxiliary.exclude_main_providers` — ungesetzt = `[nvidia]`; `[]` = altes Verhalten; lokales NIM (`NVIDIA_BASE_URL` auf anderem Host) nicht betroffen; `custom` auf `integrate.api.nvidia.com` betroffen. Explizites `auxiliary.<task>.provider: nvidia` und `/refine` gehen weiter an NVIDIA | `website/docs/user-guide/configuration.md` |
| 404 als Text | `404 page not found` (`text/plain`) auf `https://integrate.api.nvidia.com/v1/chat/completions` → `model_not_found` (nicht retrybar, Fallback); Kurzmeldung `HTTP 404: 404 page not found — NVIDIA NIM does not serve this model id (check it with `hermes model`)`. Gleicher Text ohne `/v1` oder lokal bleibt `unknown` | `agent/error_classifier.py`, `agent/api_error_summary.py` |
| 401 problem+json | Kurzmeldung `HTTP 401: Unauthorized — Invalid JWT serialization: …` statt Roh-JSON (allgemein für `{"title","detail"}`/`{"detail"}`) | `agent/api_error_summary.py` |

Bestehende Default-Configs (`DEFAULT_CONFIG`, Profile `default`/`werkbank`/`hermes_dev`) sind unverändert;
der neue Schlüssel wird nur gelesen. Wirkung nur, wenn jemand NVIDIA als Hauptprovider wählt.

## Profil anlegen (Owner)

```bash
hermes profile create nvidia-trial --clone-from werkbank \
  --description "Geeignet für: NVIDIA-Trial-Tests, nur Entwicklungsprojekte"
# Overlay-Schlüssel aus nvidia-trial-profile.config.yaml in
# ~/.hermes/profiles/nvidia-trial/config.yaml übernehmen (model, fallback_providers,
# memory, auxiliary.exclude_main_providers, delegation.models)
hermes -p nvidia-trial auth add nvidia        # Schlüssel selbst hinterlegen
```

- `--clone-from` kopiert `config.yaml`, `.env`, `SOUL.md`, Skills (Messaging-Kanäle nicht). Die Claude-/Codex-
  Zugänge für die Fallback-Kette müssen im Profil vorhanden sein (geklonte `.env`/`auth add`).
- `SOUL.md` des Klons vor dem ersten Lauf auf Personendaten prüfen; Memory und User-Profil sind in der Vorlage aus.
- Nur in Entwicklungsprojekten starten (Trial-Regeln); keine Belege, Mails, Kundenrepos.

## Fallback: unpinned vs. `--provider/-m`-Pin

Code (19.10.2026, dieser Branch): CLI (`cli_init_mixin.py:295`, pro Turn `cli_chat_turn_mixin.py:43`),
Oneshot (`oneshot.py:590`) und Gateway übergeben `get_fallback_chain(config)` an `AIAgent` — unabhängig davon,
ob Provider/Modell per `--provider/-m` gesetzt wurden. Ein Pin schaltet die Kette also **nicht** ab.
Ausnahme bleibt: gepinnte Delegations-Kinder bekommen keine Kette, außer `delegation.models.<name>.fallback_providers`
ist gesetzt (deshalb steht sie in der Vorlage an jedem Eintrag). Live-Beleg: siehe Abschnitt „Live-Probe“.

## Live-Probe (19.10.2026, Code dieses Branches über `AIAgent`)

Ohne Memory, Kontextdateien, SOUL und Background-Review; Fallback-Kette `[anthropic/claude-sonnet-5-5]`;
Schlüssel nur über Hermes' `resolve_runtime_provider("nvidia")`. HTTP-Zählung per `httpx`-Hook (nur Host/Pfad/Status).

| Probe | NVIDIA-Requests | Ergebnis |
|---|---|---|
| A: Pin `provider=nvidia, model=nvidia/does-not-exist-wb1216`, Prompt „PONG“ | 1× `POST /v1/chat/completions` → **404 `text/plain`** (0,2 s) | `⚠️ Model fallback: nvidia/does-not-exist-wb1216 via nvidia unavailable (model not found); using claude-sonnet-5-5 via anthropic.` → Antwort „PONG“ von Claude, kein Retry auf NVIDIA → **Kette greift beim Pin** |
| B: Super + Werkzeug `terminal` (`ls`), Streaming-Callback | 2× chat (200, `text/event-stream`, 0,55 s / 0,37 s) + 1× `GET /v1/models` (Kontext-Lookup) | Tool-Rundlauf ok (`api_calls=2`), 26 Stream-Deltas; danach `generate_title(...)` mit NVIDIA-Laufzeit: **0 Requests**, Titel `None` (Default-Config ohne `fallback_providers` → aus) |

NVIDIA gesamt: **4 HTTP-Requests (3 Chat + 1 `GET /v1/models`)**, dazu 1 Claude-Chat (+2 Claude-Metadaten-GETs).
Unpinned (Modell aus `model.default`) läuft im Code über denselben `get_fallback_chain(config)`-Pfad; live nicht
separat gemessen (Budget). Streaming in CLI/TUI nicht visuell geprüft — nur der Delta-Callback.

**Befund Datenhygiene:** Das `terminal`-Werkzeug lief nicht im Scratch-Ordner (Prozess-cwd), sondern im
konfigurierten Terminal-Startordner (Home): die `ls`-Ausgabe (Ordnernamen) ging als Tool-Ergebnis an NVIDIA.
Für Trial-Läufe `TERMINAL_CWD`/`terminal.cwd` auf ein Wegwerf-Verzeichnis setzen.

## Kontext / Kompression

`/v1/models` liefert kein Kontextfenster; WB-1215 hat es nicht gemessen → **unbekannt**. Die Vorlage setzt
deshalb weder `context_length` noch eine Kompressionsschwelle; Hermes nimmt statisch 131 072 für `nemotron*`.
`default_max_tokens=16384` liegt unter der gemessenen Gateway-Grenze `max_tokens ≤ 1 048 576` (Super/VL).

## Nicht in der Vorlage

- `nim-coder` → `deepseek-ai/deepseek-v4.1-flash`: in WB-1215 3× Timeout ohne Byte → nicht bestätigt.
- Antwort-ID des VL-Modells ist `private/nvidia/nemotron-3.5-super-120b-a12b` — Code, der `response.model`
  mit der angefragten ID vergleicht, greift daneben (nicht angefasst).

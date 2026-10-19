# NVIDIA Build (NIM) – Live-Smoke für Hermes (WB-1215)

- **Gemessen:** 19.10.2026, 02:11–02:23 CEST, Endpunkt `https://integrate.api.nvidia.com/v1`
- **Schlüssel:** Hermes-Credential-Pool, Profil `default`, Eintrag „NVIDIA Build (Trial)“ (`source=manual`). Wert nie gelesen, ausgegeben oder gespeichert; alle Ausgaben laufen durch einen Redaktor.
- **Weg:** `scripts/nvidia_nim_smoke.py` über Hermes' eigene Auflösung (`resolve_runtime_provider("nvidia")`, `providers.get_provider_profile("nvidia").prepare_messages`, Header aus `build_nvidia_nim_headers` → `X-BILLING-INVOKE-ORIGIN`), OpenAI-SDK 2.24.0, `max_retries=0`, Timeout 180 s. Eigenes HTTP nur für `GET /v1/models`.
- **Gegenprobe CLI:** `hermes chat -q "Smoke test. Reply with exactly the word: PONG" --provider nvidia -m <id> --oneshot -Q --ignore-rules --ignore-user-config -t todo --max-turns 2` aus einem leeren Scratch-Ordner (ohne Memory/AGENTS.md/Nutzerconfig → keine Personendaten im Systemprompt).
- **Prompts:** nur Test-Texte („PONG“, „zähle 1–5“, fiktive Stadt „Testhausen“ mit Fake-Werkzeug, Repo-Bild `assets/banner.png`). Keine Kunden- oder Personendaten.
- **Schlüssel-Check:** `git grep` nach dem NVIDIA-Key-Präfix über `docs/notes` und `scripts/nvidia_nim_smoke.py` leer. Repo-weit trifft `git grep` nur vorbestehende Fake-Werte in `tests/` und `evals/` (schon auf `main`).
- **NVIDIA-Aufrufe gesamt: 31** (25 Skript inkl. 2× `GET /models`, 6 CLI = 3 Chat + 3 automatische Titel-Erzeugung). Rohdaten: `nvidia-nim-smoke-2026-10.json`.

## Modellliste (`GET /v1/models`, 02:11:44, 200, 0,24 s)

81 Modelle. Felder je Eintrag nur `id, object, created, owned_by` — **keine Kontextlänge, keine Fähigkeiten**. `GET /v1/models/{id}` liefert dieselben 4 Felder.

| Pilot-ID (exakt wie API) | in Liste |
|---|---|
| `deepseek-ai/deepseek-v4.1-flash` | ja |
| `nvidia/nemotron-3-super-120b-a12b` | ja |
| `nvidia/nemotron-3-ultra-550b-a55b` | ja |
| `nvidia/nemotron-3.5-super-vl-120b-a12b` | ja |

Ähnliche Alternativen (Auszug): `nvidia/nemotron-3.5-lightning-30b-a3b`, `nvidia/nemotron-nano-3-30b-a3b`, `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`, `moonshotai/kimi-k2.6`, `z-ai/glm-5.3`, `z-ai/glm-5.3-flash`, `openai/gpt-oss-20b`.
Plugin-`fallback_models`: `nvidia/llama-3.1-nemotron-70b-instruct` vorhanden, **`nvidia/llama-3.3-70b-instruct` nicht vorhanden** (veraltet).

## Ergebnis je Pilotmodell

| Modell | Chat (Latenz) | Modell-ID in Antwort | Streaming (1. Chunk / 1. Inhalt / gesamt) | Tool-Rundlauf | Kontext | Vision | CLI |
|---|---|---|---|---|---|---|---|
| `deepseek-ai/deepseek-v4.1-flash` | **Timeout 180 s** (02:14:59) | – | **Timeout 180 s**, kein Byte (02:18:00, 02:23:29) | nicht getestet | nicht gemessen | – | übersprungen |
| `nvidia/nemotron-3-super-120b-a12b` | ok, 1,19 s, „PONG“ | identisch | ok 0,41 / 1,19 / 1,26 s, 21 Chunks, Usage im letzten Chunk | ok (1,39 s + 1,55 s), Ergebnis 17.5 korrekt zitiert | nicht gemessen¹ | – | ok „PONG“, API 1,2 s |
| `nvidia/nemotron-3-ultra-550b-a55b` | ok, 0,92 s, „PONG“ | identisch | ok 0,45 / 0,64 / 0,67 s, 12 Chunks | ok (0,94 s + 1,64 s) | nicht gemessen² | – | ok „PONG“, API 1,4 s |
| `nvidia/nemotron-3.5-super-vl-120b-a12b` | ok, 0,51 s, „PONG“ | **`private/nvidia/nemotron-3.5-super-120b-a12b`** | ok 0,35 / 0,64 / 0,68 s, 24 Chunks | ok (3,02 s + 0,48 s) | nicht gemessen¹ | **ok** (banner.png, 2,18 s, Text „HERMES-AGENT“ erkannt) | ok „PONG“, API 1,2 s |

Alle drei Nemotron-Modelle sind Reasoning-Modelle: `reasoning_content` kommt mit (24–177 Reasoning-Tokens bei Mini-Prompts), Super/VL melden `completion_tokens_details.reasoning_tokens`, Ultra liefert `completion_tokens_details: null`.

¹ `max_tokens=10000000` → **400** `Validation: Max tokens must not exceed 1048576, got 10000000` (`nvcf-status: errored`). Das ist eine Gateway-Validierung von `max_tokens`, **kein** Kontextfenster.
² Ultra nimmt `max_tokens=10000000` ohne Fehler an (Antwort „OK“, 2,59 s) — keine serverseitige Obergrenze sichtbar.
Kontextfenster bleibt damit **unbekannt**; Hermes nimmt statisch 131 072 für `nemotron*` und 1 000 000 für `deepseek-v4.1-flash` an (`agent/model_metadata.py:355`, `:314`) — beides am NIM-Endpunkt unbestätigt.

## Tool-Calling / `prepare_messages`

- Schritt 1 liefert bei allen drei Nemotrons `finish_reason=tool_calls` mit `get_test_temperature({"city":"Testhausen"})`.
- Schritt 2 mit Hermes-Tool-Nachricht inkl. `name`/`tool_name` → durch `prepare_messages` gestrippt → ok.
- Gegenprobe **ohne** Strippen (nur `nemotron-3-super`, 02:20:08): ebenfalls **200 ok**. Der Strip ist für dieses Modell heute nicht nötig, aber harmlos → kein Regressionstest/Fix erforderlich.

## Fehler- und Limitverhalten (wörtlich, gekürzt)

| Fall | Status | Text | Header |
|---|---|---|---|
| Fake-Schlüssel | 401 `application/problem+json` | `{"status":401,"title":"Unauthorized","detail":"Invalid JWT serialization: Missing dot delimiter(s)"}` | nur `date`, `content-type` |
| Unbekanntes Modell `nvidia/does-not-exist-wb1215` | 404 `text/plain` | `404 page not found` (kein JSON!) | `x-content-type-options` |
| `max_tokens` zu groß | 400 | `Validation: Max tokens must not exceed 1048576, got 10000000` | `nvcf-reqid`, `nvcf-status: errored` |
| DeepSeek v4.1-flash | – | SDK `APITimeoutError` nach 180 s, keine HTTP-Antwort | – |

- **Kein 429 und kein 5xx aufgetreten.** Keine Antwort enthielt `Retry-After` oder `x-ratelimit-*`; erfolgreiche Antworten tragen nur `nvcf-reqid` und `nvcf-status: fulfilled`. Rate-Limit damit **unbekannt** (keine Lastprobe, absichtlich).

## Entscheidungen

- Ticket-Pfad `docs/notes/nvidia-nim-smoke-2026-10.{md,json}` statt `docs/dev/…` aus dem Auftrag (Fertig-wenn des Tickets ist maßgeblich).
- Kontext nur per Metadaten + `max_tokens`-Validierung, kein Fülltext-Prompt (Auftrag: Trial schonen, kein Riesenprompt). Ergebnis ehrlich „unbekannt“.
- CLI-Gegenprobe mit `--ignore-rules --ignore-user-config` aus leerem Ordner, damit Memory/USER-Profil nicht an NVIDIA geht.
- DeepSeek nach 3 Timeouts nicht weiter probiert (Tools/Kontext/CLI) — jeder weitere Versuch kostet bis zu 180 s und Trial-Aufrufe ohne Erkenntnisgewinn.
- `fallback_models` im Plugin **nicht** geändert (Ticket: gehört zu WB-1216).

## Verworfen

- Steigende Fülltext-Prompts (8k–128k) zur Kontextmessung — Auftrag verbietet Riesenprompts; Folgeticket nötig, falls die Zahl gebraucht wird.
- Rate-Limit provozieren — Trial-Regeln.
- Plugin-Fix in diesem Ticket — laut Ticket WB-1216.

## Gelernt

- Die Hilfsaufgabe **Titel-Erzeugung folgt dem Hauptanbieter**: jede CLI-Sitzung schickte einen zweiten Aufruf (Titel aus dem Nutzerprompt) an NVIDIA (`Auxiliary title_generation: using nvidia …`). Für Trial-Daten-Hygiene relevant: Was im Prompt steht, geht doppelt raus.
- Das VL-Modell meldet in `model` eine andere, interne ID (`private/nvidia/nemotron-3.5-super-120b-a12b`). Code, der `response.model` mit der angefragten ID vergleicht oder danach abrechnet/Kontext nachschlägt, greift daneben.
- `GET /v1/models` ist zur Fähigkeitserkennung wertlos (keine Kontext-/Tool-/Vision-Felder).
- Unbekannte Modell-IDs liefern `404 page not found` als Klartext — Hermes' Fehlerklassifizierung sollte das als „Modell unbekannt“, nicht als Endpunktfehler deuten (für WB-1217/1218 prüfen).
- „Gelistet“ heißt nicht „bedienbar“: DeepSeek v4.1-flash steht in der Liste, antwortete aber in 3 Versuchen (02:12–02:23) gar nicht.

## Empfehlung / Folgeticket-Vorschläge (WB-1216)

1. `fallback_models` auf die drei funktionierenden Nemotron-IDs umstellen (Super als Standard), `nvidia/llama-3.3-70b-instruct` entfernen; DeepSeek erst nach erneuter Probe aufnehmen.
2. Kontextfenster der Pilotmodelle gezielt messen (eigenes Ticket mit Owner-Ja für größere Prompts) oder unbekannt lassen; Hermes-Statik (131 072 / 1 000 000) bis dahin nicht als gemessen behandeln.
3. Prüfen, ob Hilfsaufgaben (Titel, Kompression) bei `provider=nvidia` auf einen Nicht-Trial-Anbieter gelegt werden sollen.

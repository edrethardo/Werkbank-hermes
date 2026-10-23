# `model_profile: "auto"` – Subagent-Modell nach Schwierigkeit, NVIDIA-Trial zuerst (WB-1260)

Baut auf WB-1211 (`delegation.models`, `model_profile`) und WB-1216 (Hilfsaufgaben nie aufs Trial) auf.
Modell-ID und Endpunkt nur aus dem Smoke WB-1215 (`nvidia-nim-smoke-2026-10.md`):
`nvidia/nemotron-3-ultra-550b-a55b`, `https://integrate.api.nvidia.com/v1`, Chat + Streaming +
Tool-Rundlauf ok, Kontextfenster **unbekannt** (Hermes nimmt statisch 131 072 für `nemotron*` an).

## Config-Vorschlag (Live-Config ändert der Admin)

```yaml
delegation:
  models:
    nemotron:                                   # NVIDIA-Trial (Credential-Pool-Eintrag `nvidia`)
      provider: nvidia
      model: nvidia/nemotron-3-ultra-550b-a55b
      # kein fallback_providers: Rückfall regelt delegation.auto (einmal, auf das Anthropic-Profil der Stufe)
    haiku:
      provider: anthropic
      model: claude-haiku-5-5
    schnell:
      provider: anthropic
      model: claude-sonnet-5-5
    stark: {}                                   # Elternmodell
  auto:
    leicht: haiku                               # Stufe -> Anthropic-Profil (auch Rückfallziel)
    mittel: schnell
    schwer: stark
    bevorzugt: [nemotron]                       # Trial zuerst ...
    fuer: [leicht, mittel]                      # ... nur für diese Stufen
    trial_parallel: 2                           # gleichzeitige Trial-Kinder je Prozess, darüber Anthropic
    cooldown_minuten: 15                        # nach Trial-Fehler (429/5xx/Auth/Timeout) so lange kein Trial
    standard: false                             # true = Tasks ohne model_profile laufen wie "auto"
auxiliary:
  exclude_main_providers: [nvidia]              # Default; nicht leeren, sonst gibt auto das Trial nicht frei
```

Ergebnis: leicht → nemotron (sonst haiku), mittel → nemotron (sonst schnell), schwer → stark.

## Verhalten

| Punkt | Regel | Code |
|---|---|---|
| Aufruf | `tasks[].model_profile: "auto"`, optional `schwierigkeit: leicht\|mittel\|schwer`, `vertraulich: true`. Felder stehen im Schema nur, wenn `delegation.auto` gesetzt ist. Ein echtes Profil namens `auto` in `delegation.models` gewinnt. | `tools/delegate_tool.py` `_resolve_task_model_profiles`, `_plan_auto_route` |
| Prüfung vor Start | Stufe ohne Profil, unbekanntes Profil in `auto`/`bevorzugt`, ungültige `schwierigkeit` → Fehler, kein Kind startet. | `delegation_auto.validate_auto_config`, `task_stage` |
| Heuristik (ohne `schwierigkeit`) | in dieser Reihenfolge, ganze Wörter, ohne Groß/Klein: Schwer-Wort im Ziel (review, security/sicherheit, architektur/architecture, audit, threat, vulnerability, exploit, concurrency, race, migration, refactor(ing), design/entwurf, proof/beweis) → **schwer**; Ziel+Kontext ≥ 6000 Zeichen → **schwer**; Leicht-Wort (list/liste/auflisten, summarize/zusammenfassen/fasse, translate/übersetzen, rename/umbenennen, count/zählen, format, lookup/nachschlagen, grep, find/finde, search/suche) und < 1500 Zeichen → **leicht**; < 80 Zeichen → **leicht**; sonst **mittel**. Kein Modellaufruf. Grund steht im Ergebnis (`auto_grund`). | `delegation_auto.classify` |
| Trial-Vorrang | Trial nur, wenn: Stufe in `fuer`, nicht `vertraulich`, kein Cooldown, Hilfsaufgaben des Kinds bleiben vom Trial fern (`auxiliary.exclude_main_providers` schließt die Trial-Route aus, WB-1216), Credential-Pool gesund (Eintrag vorhanden, verfügbar, kein 429/5xx innerhalb des Cooldowns — nur Status-Felder, nie der Schlüssel), freier Parallel-Slot. Sonst Anthropic-Profil der Stufe; der Grund steht in `auto_hinweis`. | `_plan_auto_route`, `delegation_auto.pool_refusal`, `side_tasks_refusal` |
| `vertraulich: true` | nie aufs Trial — auch nicht per explizitem `model_profile: nemotron`, per `delegation.provider: nvidia` oder geerbt von einem Eltern-Agent auf dem Trial (dann Fehler vor Start). **Trial** heißt: was WB-1216 ausschließt (`auxiliary.exclude_main_providers`) **und unabhängig davon** jede Route eines `delegation.auto.bevorzugt`-Profils — gleicher Endpunkt-Host (ein `nvidia`-Profil ohne `base_url` = gehosteter NIM `integrate.api.nvidia.com`, auch unter anderer Provider-ID), gleicher Provider (bei geteilten Providern `openrouter`/`custom*`/`nous` nur dasselbe Modell). | `_confidential_route_error`, `delegation_auto.trial_route_check` |
| `vertraulich` und Fallback-Kette | Die Primärroute allein reicht nicht: aus der Fallback-Kette des Kinds (eigene `fallback_providers` des Profils **oder** die geerbte Kette des Eltern-Agents) werden alle Trial-Einträge entfernt; bleibt nichts übrig, läuft das Kind ohne Fallback (kein Fehler — die Primärroute ist ja kein Trial). Gilt für explizite Profile, `auto` und Tasks ohne Profil. | `_build_child_agent(drop_fallback=…)`, `delegation_auto.drop_trial_fallbacks` |
| `vertraulich` vererbt | Ein Kind eines vertraulichen Tasks trägt `_delegate_confidential`; delegiert es weiter, gelten **alle** seine Tasks als vertraulich (Enkel, Urenkel …): `auto` wählt nie das Trial, explizites Trial-Profil → Fehler, Fallback-Kette ohne Trial. | `delegation_auto.is_confidential(task, parent_agent)`, `_confidential_plan` |
| Rückfall | Trial-Lauf mit `failure_reason` auth/auth_permanent/billing/rate_limit/upstream_rate_limit/overloaded/server_error/timeout (ohne Klassifikation: HTTP 401/403/429/5xx bzw. „timed out“ im Fehlertext, Status `timeout`) **und** ohne Tool-Call → derselbe Task startet **einmal** auf dem Stufenprofil; der Fehler startet den Cooldown. Nach einem Tool-Call kein Neustart (Seiteneffekte). In einer Async-Unit ersetzt das Rückfall-Kind das Trial-Kind in `unit.children` (das `interrupt_fn` der Unit liest live → unit-weites `stop` erreicht es), behält dessen `subagent_id` (die an das Modell gemeldeten IDs bleiben gültig), bleibt wie seine Geschwister aus dem Eltern-Interrupt heraus, und das `Model:`-Label der Unit wird nachgeführt. Kam das `stop` schon vor dem Neustart, startet kein Rückfall-Kind. | `delegation_auto.run_auto_child`, `fallback_reason`, `_dispatch_unit`, `async_delegation.update_unit_model` |
| Parallelgrenze | `trial_parallel` (Vorgabe 2) Trial-Kinder gleichzeitig je Prozess; Slot wird beim Planen belegt und nach Lauf/Rückfall/Abbruch vor Start freigegeben — auch bei jeder Ausnahme zwischen Planung und Übergabe an den Batch (Budget, Live-Transkript, Kind-Bau; `try/finally`). Verwaiste Slots verfallen nach 6 h. | `try_acquire_slot`, `_release_auto_slots` |
| Transparenz | Mit `delegation.auto` trägt **jedes** Ergebnis `model` + `provider`, Auto-Tasks zusätzlich `model_profile`, `schwierigkeit`, `auto_grund`, ggf. `auto_hinweis`, `fallback_von`, `fallback_grund`. Async-Abschlussblock: `Model:` nennt `modell via provider` je Kind statt `?`, je Task eine Zeile `Model: … via … (fallback_von: …)`. | `_annotate`, `_unit_model_label`, `process_registry_notifications._format_batch_delegation` |
| Abwärtskompatibel | Ohne `delegation.auto`: Schema, Kind-Argumente, Ergebnisfelder und Texte unverändert; `model_profile: "auto"` bleibt „unknown model_profile“. | Tests `test_without_auto_*`, WB-1211-Suite |

## Ist-Befund vor WB-1260

- Modellwahl: `delegate_task` → `_resolve_delegation_credentials(routing_cfg)` (call-weit, `tools/delegate_tool.py:599`), dann je Task `_resolve_task_model_profiles` (`:612`, nur explizite Namen) und `_build_children` (`:487-490`).
- `Model: ?`: `tools/process_registry_notifications.py:155` druckt `evt['model'] or '?'`; `evt['model']` kommt aus `_unit_model_label` (`tools/delegate_tool_dispatch.py:387-393`) = `unit.creds["model"]`, und das ist `None`, sobald `delegation.model` leer ist und kein Task ein Profil hat (Kind erbt das Eltern-Modell). Die Ergebnis-Einträge hatten `model`, aber nie `provider`.

## Grenzen / offen

- Das Trial-Kind bekommt Ziel, Kontext und alle Tool-Ausgaben (Dateien, `ls` im `terminal.cwd`) — `vertraulich` ist die einzige Inhaltsbremse; Daten-Hygiene liegt beim Eltern-Agent.
- Cooldown und Slots sind prozesslokal (Gateway und CLI zählen getrennt).
- Das Rückfall-Kind wird im Worker-Thread gebaut (seriell über ein Lock). Ohne Test: der Fall, dass ein `stop` genau während des Baus eintrifft (Kind wird dann zurückgetauscht und geschlossen), und das Nicht-Anhängen ans Eltern-`_active_children` in der Async-Unit.
- Trial-Erkennung für `vertraulich` ist bewusst grob: ein `bevorzugt`-Profil mit eigenem Provider (z. B. `nvidia`) macht **jede** Route dieses Providers zur Trial-Route, auch einen selbst gehosteten NIM über die Provider-ID `nvidia` (dann `vertraulich` → Fehler bzw. Fallback-Eintrag entfernt). Selbst gehostete Endpunkte unter eigener Provider-ID (`custom`) sind davon nicht betroffen.
- `vertraulich` schützt Modell-Route und Fallback-Kette des Kinds; wohin seine Hilfsaufgaben (Kompression, Titel, Review) gehen, regelt weiter allein WB-1216 (`auxiliary.exclude_main_providers`).
- Die Vererbung von `vertraulich` hängt am Kind-Objekt (`_delegate_confidential`); ein Kind, das neu gebaut wird (z. B. Wiederaufnahme nach Prozess-Neustart), verliert das Flag.
- Kontextfenster von Nemotron Ultra am NIM-Endpunkt weiter unbestätigt (WB-1215).

# MEDIA:-Bilder im TUI-Transcript — Stand der Fehlersuche

Symptom (vom Nutzer gemeldet, 19.09.2026):

* **PC / Browser-Pane (`/p/i3`)**: Das Bild wird gemalt, landet aber **ganz oben im Pane**
  (Zeile 0), nicht an der Stelle im Transcript, zu der es gehört.
* **iPhone**: Gar kein Bild.

## Was gemessen und BEWIESEN ist

| Stufe | Sonde | Ergebnis |
|---|---|---|
| Producer | `tui_gateway.transcript_images.render_image(path, 'kitty')` direkt | 20854 Bytes, `c=80,r=24` — **grün** |
| RPC | Produktions-Mitschrift `/tmp/img_debug.log` aus einer echten Sitzung | `proto=kitty available=true seq=20854 isTTY=true`, `writeAbove=true` — **grün** |
| Consumer (isoliert) | `scripts/e2e/consumer_image_probe.mjs` — das xterm.js + `@xterm/addon-image` des i3-Plugins, gefüttert mit den ECHTEN Bytes | Handler `['iip','kitty']` aktiv, **311904** gemalte Pixel — **grün** |
| Cursor | `scripts/e2e/kitty_cursor_probe.mjs` | Cursor nach dem Bild korrekt auf `(80,24)`; Text danach überschreibt es nicht — **grün** |
| writeAbove-Rahmen | `scripts/e2e/write_above_consumer_probe.mjs` — `\r` + `ESC[<frameRows>A` + `ESC[J` + Bild + `\n`, danach Frame-Repaint | Bild bleibt, aber Pixelzahl fällt von 311904 auf **272916** → der Repaint knabbert es an |

## Die eigentliche Ursache (bekannt, im Skill dokumentiert)

`Ink.writeAbove()` (`ui-tui/packages/hermes-ink/src/ink/ink.tsx:1378`) springt um
`frontFrame.screen.height - 1` Zeilen HOCH und schreibt dort. Solange die Sitzung noch auf
einen Bildschirm passt, **beginnt Inks Frame beim Banner** — der Sprung landet also auf Zeile 0
und nicht neben dem zugehörigen Text. Genau das beschreibt der Nutzer.

Der im Skill `hermes-tui-ink-development` festgehaltene Ausweg: die Zellen müssen **zum Frame
gehören**. Also ein eigener `Msg.kind`, ein leerer `<Box height={n}>` als reservierter Block,
und die Escape-Sequenz in diesen Block schreiben. Dann kennt Ink die Zeilen und räumt sie beim
Repaint korrekt. Die Höhe muss an ZWEI Stellen gleich sein: `appLayout.tsx` und
`lib/virtualHeights.ts::estimatedMsgHeight`.

## Offen / unbewiesen

* **iPhone**: Warum dort GAR nichts erscheint, ist nicht gemessen. Safari/iOS + kitty-Handler
  im selbstgebauten xterm.js ist ungeprüft; der isolierte Consumer-Probe lief nur in Chromium.
* Ein Roh-Schreiben der Sequenz direkt in die PTY (`cat seq > /dev/pts/7`) zeigte beim Nutzer
  **nur den Text daneben, kein Bild** — das widerspricht dem grünen Consumer-Probe und ist
  **nicht aufgelöst**. Möglich, dass das Bild auch dabei oben im Pane landete und übersehen
  wurde. Diese eine Messung braucht eine Wiederholung mit Blick auf den GESAMTEN Pane.

## Instrumentierung

`ui-tui/src/app/createGatewayEventHandler.ts` schreibt derzeit bewusst **ungated** nach
`/tmp/img_debug.log` (eine Zeile pro Bild: Protokoll, `available`, Byte-Zahl, `writeAbove`).
Vor dem Merge entfernen.

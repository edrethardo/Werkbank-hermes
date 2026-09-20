# MEDIA:-Bilder im TUI-Transcript — Befund und Behebung

Symptom (19.09.2026): am PC landete das Bild **ganz oben im Pane**, losgelöst vom Text;
auf dem iPhone erschien **gar keins**.

## Eine Ursache, beide Symptome

`Ink.writeAbove()` schreibt oberhalb von Inks Frame und lässt den Frame danach **von dort
abwärts** neu zeichnen. Der Repaint holt sich also genau die Zeilen zurück, in denen das
Bild liegt — anteilig zur Framehöhe.

Gemessen (`scripts/e2e/writeabove_frameheight_probe.mjs`, Schirm 30 Zeilen, Bild 80×24):

| Framehöhe | direkt nach writeAbove | nach dem Repaint |
|---|---|---|
| 6  | 311904 | 311904 (ganz) |
| 12 | 311904 | 233928 |
| 20 | 311904 | 129960 |
| 26 | 311904 |  51984 |
| 29 | 311904 |  12996 (4 %) |

Am PC ist der Frame kurz → das Bild überlebt, sitzt aber oberhalb des Frames, und solange
die Sitzung auf einen Schirm passt, beginnt der Frame beim **Banner**. "Oberhalb" ist dann
Zeile 0. Auf dem Telefon füllt der Frame den Schirm → es bleibt praktisch nichts übrig.

Dazu kam für das Telefon ein zweiter Effekt: `transcript_images.MAX_COLS` war fest 80. Ein
schmaler Pane schneidet rechts ab (`narrow_terminal_probe.mjs`: bei 32 Spalten blieben von
311904 Pixeln noch 52364).

## Behebung

* **`Ink.writeIntoFrame(marker, payload)`** (neu, `packages/hermes-ink/src/ink/ink.tsx`):
  malt in einen Block, den der Frame **selbst reserviert** hat. Der Aufrufer rendert eine
  leere `<Box height={n}>` mit einem Marker in der ersten Zeile; Ink sucht die Markerzeile
  im gerade gezeichneten Frame und fährt relativ dorthin. Die Zellen gehören damit dem
  Frame und überleben jeden Repaint.
* **Marker aus U+2800** (Braille-Blank): ein echtes, leeres Zeichen. Zero-width-Zeichen
  (U+200B, U+2063) gehen **nicht** — Ink verwirft sie beim Rendern, sie erreichen die
  Zellen nie (gemessen, war zuerst ein stiller Fehlschlag).
* **Terminalmaße reisen mit der RPC.** Das Gateway kann sie nicht kennen: bei einer
  angehängten Sitzung läuft es auf einer anderen Maschine. `image.terminal_sequence` nimmt
  jetzt `cols`/`rows`, `render_image` deckelt darauf und behält das Seitenverhältnis.

## Live belegt

`scripts/e2e/image_placement_live.mjs` gegen eine echte TUI in einem echten i3-Pane,
gemessen wird das Zeilenband des Bild-Layers (nicht bloß "Pixel existieren"):

```
pc      (1280x900)  216720 Pixel, Band 435–794 von 870  → im Textfluss
telefon (390x700)    71760 Pixel, Band 403–610 von 676  → im Textfluss
```

Screenshots bestätigen visuell: das Bild steht unter "Hier ist es: /tmp/kitty_test.png",
vollständig, auf dem schmalen Pane in die Breite passend.

## Sonden in diesem Verzeichnis

| Datei | beantwortet |
|---|---|
| `consumer_image_probe.mjs` | malt das xterm.js des i3-Plugins die echten Bytes? (auch `ENGINE=webkit`) |
| `narrow_terminal_probe.mjs` | was passiert bei zu schmalem Terminal? |
| `writeabove_frameheight_probe.mjs` | wie viel frisst der Repaint, je Framehöhe? |
| `kitty_cursor_probe.mjs` | wo steht der Cursor nach der Sequenz? |
| `write_above_consumer_probe.mjs` | die volle writeAbove-Sequenz inkl. Repaint |
| `image_placement_live.mjs` | Geometrie gegen eine echte TUI, PC und Telefon |
| `image_persistence_probe.mjs` | überlebt das Bild Tippen, Scrollen, Resize? |
| `image_persistence_turns_probe.mjs` | überlebt es weitere Antworten? |
| `image_loss_events_probe.mjs` | welches Ereignis löscht es, kommt es zurück? |
| `image_overlay_probe.mjs` | Slash-Menü, `/help`, ctrl+L |
| `image_onscreen_repaint_probe.mjs` | Nachmalen, solange der Block auf dem Schirm ist |

**Fallstrick für die nächste Sonde:** das i3-Pane nutzt den **DOM**-Renderer von xterm.js;
`.pane canvas` existiert erst, wenn ein Bild gemalt wurde. Auf `.pane .xterm` warten.

---

# Nachtrag: „einmal gezeigt, dann weg"

Symptom (19.09.2026, gleicher Tag): das Bild erschien korrekt und verschwand danach.

## Ursache: ein Malvorgang ist keine Zusicherung

`writeIntoFrame` malte **einmal**. Die Pixel gehören aber dem Terminal, nicht Inks
Zellmodell — Ink hält den (leeren) Block für unverändert und erzeugt nie wieder einen Diff
dafür. Alles, was den Schirm über diesen Zeilen neu zeichnet, löscht sie endgültig.

Gemessen (`image_loss_events_probe.mjs`, `image_overlay_probe.mjs`, 1100×800-Pane):

| Ereignis | vorher | nachher |
|---|---|---|
| Tippen, Composer mehrzeilig, Scrollen | 216720 | 216720 (bleibt) |
| zwei weitere Antworten | 216720 | 216720, Band wandert mit dem Text |
| Slash-Menü offen | 216720 | 93555 (verdeckt) |
| Slash-Menü zu | 93555 | **93555 — kam nicht zurück** |
| `/help` offen/zu | 216720 | **0** |
| ctrl+L | 216720 | **0** |
| Terminal-Resize | 216720 | **0**, auch nach Resize zurück |

## Behebung: registrierte Blöcke statt Einmal-Malen

`writeIntoFrame(marker, payload, rows)` **registriert** den Block. `syncFrameBlocks` läuft am
Ende jedes Frames und malt ihn nach, sobald seine Zeilen wieder frei sind. Ein Block gilt als
schmutzig, wenn der Frame den Schirm gelöscht hat (`clearTerminal`-Patch, ERASE-Pfade),
`repaint()`/`resetFramesForAltScreen()` lief, ein Resize kam, oder etwas über seinen Zeilen
steht. Ein bloß **verschobener** Block wird nicht nachgemalt: das Terminal scrollt seine
eigenen Pixel mit dem Text (gemessen — das Band wandert, die Pixelzahl bleibt gleich).

`rows` muss mitreisen: nur die erste Zeile trägt den Marker, ein Overlay über den Zeilen
DARUNTER sähe sonst aus wie ein unberührter Block.

## Zweiter Fehler, erst durch den Fix sichtbar

Der Sprung zur Markerzeile rechnete mit `screen.height - 1 - row`. Der Frame umfasst aber das
ganze **Transcript**, nicht den Schirm: live gemessen 222 Zeilen auf einem 52-Zeilen-Terminal.
Der Sprung ging 179 Zeilen hoch, das Terminal klemmte ihn auf Zeile 0 — das Bild landete über
dem `/help`-Panel. Richtig ist `cursor.y - row` (beides Frame-Koordinaten), plus eine Absage,
wenn die Zeile weiter als einen Schirm entfernt ist. Solange die Sitzung auf einen Schirm
passt, sind beide Werte gleich; deshalb fiel es erst auf, als ein Overlay ein Nachmalen
auslöste.

## Live belegt (`image_onscreen_repaint_probe.mjs`)

```
frisch           216720   Band 330-689
ctrl+L           216720   Band 330-689
Slash-Menü offen 216720   Band 330-689
Slash-Menü zu    216720   Band 330-689
Resize schmaler  217080   Band 330-689
Resize zurück    216720   Band 330-689
```

Screenshot nach dem Resize: das Bild steht unter „Hier:", vollständig, nichts überlappt.

## Was NICHT behoben ist

Ein `/help` schiebt den Block ~180 Zeilen in den **Scrollback**. Dort ist er mit relativen
Cursorbewegungen nicht erreichbar, `paintFrameBlock` sagt ab, und das Bild bleibt weg —
auch beim Zurückscrollen (belegt: `ovl_7_6_hochgescrollt.png` zeigt die Stelle leer).
Ein Fix dafür bräuchte kitty `U=1` (virtuelle Platzierung, das Terminal scrollt die Pixel
selbst mit), was `@xterm/addon-image` nicht implementiert.

**Messfallstrick:** `painted=0` heißt nicht zwingend „zerstört" — das Bild kann korrekt aus
dem Schirm gescrollt sein. Erst Hochscrollen unterscheidet das; ohne diesen Schritt hält man
gesundes Verhalten für einen Fehler.

---

# Nachtrag 2: „nach dem Safari-Reload sind beide weg"

Symptom (19.09.2026, Abend): Bilder standen korrekt, nach einem Reload in Safari am iPhone
war die Stelle leer — auch bei mehreren Bildern und auch nach Tippen.

## Ursache: ein neuer Client ist für Ink kein Ereignis

Die Bildbytes leben im Node-Prozess (`frameBlocks`, bis zu 16 Blöcke). `syncFrameBlocks` malt
sie nach — aber **nur schmutzige** Blöcke. Schmutzig wird gesetzt bei Screen-Wipe, `repaint()`,
SIGCONT und Resize **mit geändertem Maß** (`ink.tsx:542`).

Ein Browser-Reload ist nichts davon. Der Pane-Server spielt beim `attach` nur seinen
512-KiB-Ring zurück (`panes.py:121`): liegen die kitty-Bytes noch drin, erscheint das Bild —
sind sie herausgerollt, bleibt es weg, obwohl der Prozess sie noch hält. Tippen hilft nicht,
weil ein nicht-schmutziger Block nie nachgemalt wird.

## Behebung: `Pane.nudge_repaint()`

Beim `attach` (nicht beim `open` — ein frisches Pane hat nichts nachzumalen) geht ein echter
Maßwechsel in den PTY: `rows-1`, ein Frame Pause, zurück auf `rows`. Ink markiert alle Blöcke
schmutzig und malt sie im nächsten Frame neu, unabhängig vom Ring. Ein Resize auf dieselben
Werte genügt nicht — das verwirft Ink als No-op. Kam inzwischen das `refit` des Clients an,
gehören ihm die Maße und der zweite Schritt entfällt (sein Resize hat ohnehin schmutzig
markiert). Eingehängt in beide ws-Handler: `page.py` (Dashboard-Seite) und `server.py`
(standalone `hermes i3`).

## Live belegt (`image_reload_small_ring.mjs`, WebKit)

```
ohne Fix:  1_vor_reload 216720 Band 330-689  →  2_nach_reload      0  Band -1--1
mit  Fix:  1_vor_reload 216720 Band 330-689  →  2_nach_reload 216720  Band 330-689
```

## Drei Sonden, die NICHTS bewiesen haben — und warum

Der Weg hierhin bestand aus drei unschlüssigen Läufen, die wie Ergebnisse aussahen:

1. **Ring per Tastendruck füllen.** 14 Runden à 220 Zeichen ergaben 98 KB Replay; die
   Bildbytes lagen noch drin (`a=T,f=100` ×1, direkt am Ring gemessen). Der Lauf war grün,
   ohne den Fehler je zu erzeugen — und der Lauf OHNE Fix war genauso grün. **Eine Gegenprobe,
   die ebenfalls grün ist, ist kein Ergebnis, sondern der Beweis, dass die Sonde danebenmisst.**
2. **Ring per `/help` füllen.** Das füllt ihn, scrollt aber das Bild vom Schirm: `painted=0`
   schon VOR dem Reload. Rot aus dem falschen Grund.
3. **Erst danach isoliert:** `_RING_BYTES` server-seitig temporär auf 8 KiB. Der Replay ist
   garantiert leer, das Bild bleibt, wo es ist — nur eine Variable ändert sich.

Regeln, die daraus folgen und in jede weitere Bildsonde gehören:
* **Die Vorbedingung messen, nicht annehmen.** „Der Ring ist weitergerollt" ist eine Behauptung,
  bis der Ring ausgelesen wurde (`ring_check.py`: attach, ersten Byte-Schwung zählen,
  `a=T,f=100` suchen).
* **Vor dem Ereignis prüfen, dass das Bild da ist.** Die Sonde bricht ab, wenn
  `painted <= 0` vor dem Reload — sonst misst man das Wegscrollen statt des Fehlers.
* **Eine Variable je Lauf.** Statt die Umgebung vollzuschreiben, bis der Effekt eintritt,
  die eine Größe verstellen, um die es geht.
* **Safari ist die Engine des Nutzers:** `ENGINE=webkit`. Langsam tippen (`delay: 80`) und den
  Composer vor Enter prüfen, sonst kommt der Prompt verstümmelt an.


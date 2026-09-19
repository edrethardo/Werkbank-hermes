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

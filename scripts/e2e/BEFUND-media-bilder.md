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

**Fallstrick für die nächste Sonde:** das i3-Pane nutzt den **DOM**-Renderer von xterm.js;
`.pane canvas` existiert erst, wenn ein Bild gemalt wurde. Auf `.pane .xterm` warten.

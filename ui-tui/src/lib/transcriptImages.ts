/**
 * Bilder aus einer Agentenantwort im Transcript anzeigen.
 *
 * Der Agent kündigt Dateien seit jeher mit `MEDIA:/pfad` an — Telegram, Discord und der
 * Desktop liefern sie darüber aus, CLI und TUI taten bisher nichts (der Systemprompt sagte
 * dem Modell wörtlich, die Tags würden hier nicht abgefangen). Dieses Modul schließt die
 * Lücke für bildfähige Terminals.
 *
 * WARUM NICHT ALS INK-KOMPONENTE: Ink rendert den Bildschirm als Zell-Diff und muss jede
 * Zelle vermessen können. Eine Bildsequenz belegt Zellen, von denen Ink nichts weiß. Der
 * Ausweg ist derselbe wie beim Maskottchen — direkt auf stdout schreiben, an Inks Rendering
 * vorbei. Anders als das Maskottchen (ein fixiertes Overlay mit kitty-Unicode-Platzhaltern)
 * wird hier DIREKT platziert, damit das Bild im Textfluss steht und mitscrollt.
 *
 * Live gemessen (spikes/004): ein so geschriebenes Bild überlebt Inks Repaint und scrollt
 * mit dem Text. Ohne diese Eigenschaft wäre der Ansatz nicht tragfähig.
 */

/** Ein `MEDIA:`-Tag, wie der Desktop ihn erkennt (artifact-utils.ts) — inklusive der
 *  Varianten in Backticks/Anführungszeichen, die Modelle gern produzieren. */
const MEDIA_RE = /[`"']?MEDIA:\s*(`[^`\n]+`|"[^"\n]+"|'[^'\n]+'|\S+)[`"']?/g

const IMAGE_EXT_RE = /\.(png|jpe?g|gif|webp|bmp)$/i

/** Pfade aus den `MEDIA:`-Tags eines Textes, in Reihenfolge und ohne Dubletten. */
export function mediaPaths(text: string): string[] {
  const out: string[] = []

  for (const m of text.matchAll(MEDIA_RE)) {
    const raw = (m[1] ?? '').replace(/^[`"']|[`"']$/g, '').replace(/[.,;:]+$/, '')

    if (raw && !out.includes(raw)) {
      out.push(raw)
    }
  }

  return out
}

/** Nur die Pfade, die überhaupt ein Bild sein können. Alles andere bleibt Text: ein PDF
 *  lässt sich nicht anzeigen, und der Pfad ist für den Nutzer brauchbarer als nichts. */
export function imagePaths(text: string): string[] {
  return mediaPaths(text).filter(p => IMAGE_EXT_RE.test(p))
}

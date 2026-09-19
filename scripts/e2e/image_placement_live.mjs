/* Live-Beweis: steht das Bild BEI seinem Text, und ueberlebt es den Repaint —
 * in einem breiten Pane (PC) UND in einem schmalen, kurzen (Telefon)?
 *
 * Kein Modellaufruf im Messpfad: die Sonde treibt den Renderer ueber eine echte
 * TUI in einem echten i3-Pane und schickt den Text, der das Bild ausloest, als
 * Slash-freien Prompt — deshalb braucht sie einen laufenden `hermes i3`.
 *
 * Gemessen wird GEOMETRIE, nicht nur Anwesenheit: die Zeilenbaender des
 * Bild-Layers gegen die Bildschirmmitte. "N Pixel existieren" geht auch dann
 * gruen durch, wenn das Bild oben im Pane klebt.
 *
 *   node scripts/e2e/image_placement_live.mjs "http://127.0.0.1:91NN/?token=..."
 */
import { chromium } from 'playwright'

const URL = process.argv[2]
if (!URL) {
  console.error('Aufruf: node scripts/e2e/image_placement_live.mjs <pane-url>')
  process.exit(2)
}

const browser = await chromium.launch()

async function run(label, width, height) {
  const page = await browser.newPage({ viewport: { width, height } })
  const errs = []
  page.on('pageerror', e => errs.push(String(e)))

  try {
    await page.goto(URL)
    await page.waitForSelector('.pane .xterm', { timeout: 30000 })
    await page.waitForTimeout(4000)

    const before = await measure(page)

    // Den Ausloeser tippen. Text und Enter GETRENNT — zusammen bleibt die Zeile stehen.
    await page.keyboard.type('zeig mir MEDIA:/tmp/kitty_test.png')
    await page.waitForTimeout(1500)
    await page.keyboard.press('Enter')

    // Auf den Bild-Layer warten, nicht blind schlafen.
    const erschienen = await page
      .waitForFunction(() => {
        for (const c of document.querySelectorAll('canvas')) {
          if (!/image/i.test(c.className) || !c.width) continue
          const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data
          for (let i = 3; i < d.length; i += 4) if (d[i] > 8) return true
        }
        return false
      }, null, { timeout: 180000 })
      .then(() => true)
      .catch(() => false)

    await page.waitForTimeout(2500)
    const after = await measure(page)

    console.log(`\n=== ${label} (${width}x${height}) ===`)
    console.log('Bild erschienen :', erschienen)
    console.log('vorher          :', JSON.stringify(before))
    console.log('nachher         :', JSON.stringify(after))

    if (after.painted > 0) {
      const mitte = after.canvasHeight / 2
      const lage = after.top < mitte * 0.35 ? 'OBEN (falsch platziert)'
        : after.bottom > after.canvasHeight * 0.95 ? 'ganz unten'
          : 'im Textfluss'
      console.log(`Lage            : ${lage}  (Band ${after.top}–${after.bottom} von ${after.canvasHeight})`)
    }

    if (errs.length) console.log('JS-Fehler       :', errs.slice(0, 3))
    await page.screenshot({ path: `/tmp/live_${label}.png`, fullPage: false })
    console.log(`Screenshot      : /tmp/live_${label}.png`)
  } catch (err) {
    // Screenshot AUCH auf dem Fehlerpfad — sonst ist die eine erklaerende Spur weg.
    await page.screenshot({ path: `/tmp/live_${label}_fehler.png` }).catch(() => {})
    console.log(`\n=== ${label}: FEHLER ${String(err).slice(0, 200)}`)
  } finally {
    await page.close()
  }
}

/** Pixelzahl und das Zeilenband des Bild-Layers. */
function measure(page) {
  return page.evaluate(() => {
    let painted = 0, top = -1, bottom = -1, canvasHeight = 0
    for (const c of document.querySelectorAll('canvas')) {
      if (!/image/i.test(c.className) || !c.width) continue
      canvasHeight = Math.max(canvasHeight, c.height)
      const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data
      for (let y = 0; y < c.height; y++) {
        let rowHit = false
        for (let x = 0; x < c.width; x++) {
          if (d[(y * c.width + x) * 4 + 3] > 8) { painted++; rowHit = true }
        }
        if (rowHit) { if (top < 0) top = y; bottom = y }
      }
    }
    return { painted, top, bottom, canvasHeight }
  })
}

await run('pc', 1280, 900)
await run('telefon', 390, 700)

await browser.close()

/* Zweiter Teil der Persistenzfrage: ueberlebt das Bild WEITERE ANTWORTEN?
 *
 * Der Block gehoert dem Frame — aber sobald neue Nachrichten dazukommen, rutscht
 * er nach oben, und die Transcript-Virtualisierung kann ihn aus dem Fenster
 * werfen. Gemessen wird nach jeder weiteren Antwort.
 *
 *   node scripts/e2e/image_persistence_turns_probe.mjs "http://127.0.0.1:91NN/?token=..."
 */
import { chromium } from 'playwright'

const URL = process.argv[2]
if (!URL) {
  console.error('Aufruf: node scripts/e2e/image_persistence_turns_probe.mjs <pane-url>')
  process.exit(2)
}

const measure = page =>
  page.evaluate(() => {
    let painted = 0, top = -1, bottom = -1
    for (const c of document.querySelectorAll('canvas')) {
      if (!/image/i.test(c.className) || !c.width) continue
      const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data
      for (let y = 0; y < c.height; y++) {
        let rowHit = false
        for (let x = 0; x < c.width; x++) {
          if (d[(y * c.width + x) * 4 + 3] > 8) { painted++; rowHit = true }
        }
        if (rowHit) { if (top < 0) top = y; bottom = y }
      }
    }
    return { painted, top, bottom }
  })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1100, height: 800 } })
let n = 0
const notiere = async label => {
  const m = await measure(page)
  console.log(`${label.padEnd(30)} painted=${String(m.painted).padStart(7)}  Band ${m.top}-${m.bottom}`)
  await page.screenshot({ path: `/tmp/turn_${++n}_${label.replace(/\W+/g, '_')}.png` }).catch(() => {})
  return m
}

const senden = async text => {
  await page.keyboard.type(text)
  await page.waitForTimeout(1200)
  await page.keyboard.press('Enter')
}

try {
  await page.goto(URL)
  await page.waitForSelector('.pane .xterm', { timeout: 30000 })
  await page.waitForTimeout(4000)

  await senden('zeig mir MEDIA:/tmp/kitty_test.png')
  const erschienen = await page
    .waitForFunction(() => {
      for (const c of document.querySelectorAll('canvas')) {
        if (!/image/i.test(c.className) || !c.width) continue
        const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data
        for (let i = 3; i < d.length; i += 4) if (d[i] > 8) return true
      }
      return false
    }, null, { timeout: 240000 })
    .then(() => true)
    .catch(() => false)

  console.log('Bild erschienen:', erschienen)
  if (!erschienen) {
    await page.screenshot({ path: '/tmp/turn_kein_bild.png' })
    console.log('ABBRUCH: unschluessig, kein Bild.')
    await browser.close()
    process.exit(1)
  }
  await page.waitForTimeout(2500)
  await notiere('0_frisch')

  for (const [i, frage] of [['a', 'sag nur OK'], ['b', 'sag nur FERTIG']]) {
    await senden(frage)
    // Auf das Ende des Turns warten: der Composer ist wieder leer und der
    // Bildschirmtext enthaelt die Antwort. Grob per Zeit, aber grosszuegig.
    await page.waitForTimeout(45000)
    await notiere(`turn_${i}_danach`)
  }
} catch (err) {
  await page.screenshot({ path: '/tmp/turn_fehler.png' }).catch(() => {})
  console.log('FEHLER:', String(err).slice(0, 300))
} finally {
  await browser.close()
}

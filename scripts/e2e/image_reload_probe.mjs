/* Ueberlebt ein MEDIA:-Bild einen BROWSER-RELOAD (Safari/Chrome neu laden)?
 *
 * Auf dem Reload ist der xterm.js frisch: der Bildspeicher des Emulators ist leer,
 * wiedergegeben wird nur der Scrollback-Ring (i3-Pane) bzw. der Ring + ein ctrl+L
 * (Dashboard-Chat). Die Frage ist, ob Inks syncFrameBlocks den Block nachmalt.
 *
 *   node scripts/e2e/image_reload_probe.mjs "http://127.0.0.1:91NN/?token=..."
 */
import { chromium, webkit } from 'playwright'

// Aaron sieht den Fehler in Safari — also muss die Sonde auch WebKit fahren koennen.
const ENGINE = process.env.ENGINE === 'webkit' ? webkit : chromium
const URL = process.argv[2]
if (!URL) {
  console.error('Aufruf: node scripts/e2e/image_reload_probe.mjs <pane-url>')
  process.exit(2)
}

const measure = page =>
  page.evaluate(() => {
    let painted = 0, top = -1, bottom = -1, canvases = 0
    for (const c of document.querySelectorAll('canvas')) {
      if (!/image/i.test(c.className) || !c.width) continue
      canvases++
      const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data
      for (let y = 0; y < c.height; y++) {
        let rowHit = false
        for (let x = 0; x < c.width; x++) {
          if (d[(y * c.width + x) * 4 + 3] > 8) { painted++; rowHit = true }
        }
        if (rowHit) { if (top < 0) top = y; bottom = y }
      }
    }
    return { painted, top, bottom, canvases }
  })

const browser = await ENGINE.launch()
const page = await browser.newPage({ viewport: { width: 1100, height: 800 } })
let n = 0
const notiere = async (label, ms = 2000) => {
  await page.waitForTimeout(ms)
  const m = await measure(page).catch(() => ({ painted: -1, top: -1, bottom: -1, canvases: -1 }))
  console.log(`${label.padEnd(30)} painted=${String(m.painted).padStart(7)}  Band ${m.top}-${m.bottom}  canvas=${m.canvases}`)
  await page.screenshot({ path: `/tmp/reload_${++n}_${label.replace(/\W+/g, '_')}.png` }).catch(() => {})
  return m
}

try {
  await page.goto(URL)
  await page.waitForSelector('.pane .xterm', { timeout: 30000 })
  await page.waitForTimeout(4000)

  /* WebKit verschluckt bei voller Geschwindigkeit Zeichen (gemessen: aus
   * "MEDIA:/tmp/kitty_test.png" wurde "MEDIA:/tmp/tt_test.pg") — der Lauf ist
   * dann unschluessig, nicht rot. Also langsam tippen und den Composer prüfen,
   * bevor Enter kommt. */
  const PROMPT = 'zeig mir MEDIA:/tmp/kitty_test.png'
  await page.keyboard.type(PROMPT, { delay: 80 })
  await page.waitForTimeout(1500)
  const sichtbar = await page.evaluate(() => document.querySelector('.pane .xterm')?.innerText ?? '')
  if (!sichtbar.includes('/tmp/kitty_test.png')) {
    console.log('ABBRUCH: Eingabe kam verstuemmelt an — unschluessig, kein Befund.')
    await page.screenshot({ path: '/tmp/reload_eingabe_kaputt.png' }).catch(() => {})
    await browser.close()
    process.exit(1)
  }
  await page.keyboard.press('Enter')

  const erschienen = await page
    .waitForFunction(() => {
      for (const c of document.querySelectorAll('canvas')) {
        if (!/image/i.test(c.className) || !c.width) continue
        const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data
        for (let i = 3; i < d.length; i += 4) if (d[i] > 8) return true
      }
      return false
    }, null, { timeout: 300000 })
    .then(() => true)
    .catch(() => false)

  console.log('Bild erschienen:', erschienen)
  if (!erschienen) {
    await page.screenshot({ path: '/tmp/reload_kein_bild.png' })
    console.log('ABBRUCH: unschluessig (Trigger nie angekommen?).')
    await browser.close()
    process.exit(1)
  }
  await notiere('0_frisch', 2500)

  await page.reload()
  await page.waitForSelector('.pane .xterm', { timeout: 30000 })
  await notiere('1_nach_reload', 6000)

  // Tippen erzwingt Frames — holt syncFrameBlocks das Bild zurueck?
  await page.keyboard.type('abc')
  await notiere('2_tippen', 2500)
  for (let i = 0; i < 3; i++) await page.keyboard.press('Backspace')
  await notiere('3_leer', 2000)

  // Explizites ctrl+L (markFrameBlocksDirty + repaint).
  await page.keyboard.press('Control+l')
  await notiere('4_ctrl_l', 3000)
} catch (err) {
  await page.screenshot({ path: '/tmp/reload_fehler.png' }).catch(() => {})
  console.log('FEHLER:', String(err).slice(0, 400))
} finally {
  await browser.close()
}

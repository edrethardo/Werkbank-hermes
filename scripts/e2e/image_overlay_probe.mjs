/* Dritter Fehlermodus-Kandidat: OVERLAYS. Slash-Menue, Pager, Escape — sie
 * zeichnen Inks Frame neu ueber den Zeilen, in denen die Bildbytes liegen.
 *
 *   node scripts/e2e/image_overlay_probe.mjs "http://127.0.0.1:91NN/?token=..."
 */
import { chromium } from 'playwright'

const URL = process.argv[2]
if (!URL) {
  console.error('Aufruf: node scripts/e2e/image_overlay_probe.mjs <pane-url>')
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
const notiere = async (label, ms = 1500) => {
  await page.waitForTimeout(ms)
  const m = await measure(page)
  console.log(`${label.padEnd(32)} painted=${String(m.painted).padStart(7)}  Band ${m.top}-${m.bottom}`)
  await page.screenshot({ path: `/tmp/ovl_${++n}_${label.replace(/\W+/g, '_')}.png` }).catch(() => {})
  return m
}

try {
  await page.goto(URL)
  await page.waitForSelector('.pane .xterm', { timeout: 30000 })
  await page.waitForTimeout(4000)

  await page.keyboard.type('zeig mir MEDIA:/tmp/kitty_test.png')
  await page.waitForTimeout(1500)
  await page.keyboard.press('Enter')

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
    await page.screenshot({ path: '/tmp/ovl_kein_bild.png' })
    console.log('ABBRUCH: unschluessig.')
    await browser.close()
    process.exit(1)
  }
  await notiere('0_frisch', 2500)

  // Slash-Menue: ein Overlay ueber dem unteren Frame-Bereich.
  await page.keyboard.type('/')
  await notiere('1_slash_menue_offen')
  await page.keyboard.press('Escape')
  await page.keyboard.press('Backspace')
  await notiere('2_slash_zu')

  // Ein echtes Vollbild-Overlay.
  await page.keyboard.type('/help')
  await page.waitForTimeout(800)
  await page.keyboard.press('Enter')
  await notiere('3_help_offen', 3000)
  await page.keyboard.press('Escape')
  await notiere('4_help_zu', 2500)

  // Strg+L (Neuzeichnen/Clear-Kandidat).
  await page.keyboard.press('Control+l')
  await notiere('5_ctrl_l', 2500)

  /* painted=0 kann zweierlei heissen: die Pixel sind zerstoert, ODER das Bild
   * ist korrekt aus dem Schirm gescrollt (das /help-Panel ist ~150 Zeilen
   * hoch). Nur Hochscrollen unterscheidet das — ohne diesen Schritt haelt man
   * gesundes Verhalten fuer einen Fehler. */
  await page.mouse.move(550, 400)
  for (let i = 0; i < 14; i++) await page.mouse.wheel(0, -400)
  await notiere('6_hochgescrollt', 2500)
} catch (err) {
  await page.screenshot({ path: '/tmp/ovl_fehler.png' }).catch(() => {})
  console.log('FEHLER:', String(err).slice(0, 300))
} finally {
  await browser.close()
}

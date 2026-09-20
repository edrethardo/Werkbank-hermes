/* Der Fall, den Aaron hat: das Bild ist laengst nach oben in den Scrollback
 * gewandert. Es ist dann NICHT mehr in Inks Frame erreichbar (paintFrameBlock
 * verweigert Zeilen ausserhalb des Schirms) und seine Bytes fallen irgendwann
 * aus dem 512-KiB-Ring des Panes. Frage: ueberlebt es dort einen Reload?
 *
 * Messung jeweils NACH dem Hochscrollen zum Bild, nicht am unteren Rand.
 *
 *   node scripts/e2e/image_reload_scrollback_probe.mjs "http://127.0.0.1:91NN/?token=..."
 */
import { chromium } from 'playwright'

const URL = process.argv[2]
if (!URL) {
  console.error('Aufruf: node scripts/e2e/image_reload_scrollback_probe.mjs <pane-url>')
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
  const m = await measure(page).catch(() => ({ painted: -1, top: -1, bottom: -1 }))
  console.log(`${label.padEnd(32)} painted=${String(m.painted).padStart(7)}  Band ${m.top}-${m.bottom}`)
  await page.screenshot({ path: `/tmp/sb_${++n}_${label.replace(/\W+/g, '_')}.png` }).catch(() => {})
  return m
}

// Scrollt schrittweise hoch und meldet den besten Messwert (Bild kann ueberall liegen).
const suchHoch = async (label, schritte = 40) => {
  await page.mouse.move(550, 400)
  let best = { painted: 0, top: -1, bottom: -1 }
  for (let i = 0; i < schritte; i++) {
    await page.mouse.wheel(0, -300)
    await page.waitForTimeout(120)
    const m = await measure(page).catch(() => ({ painted: 0, top: -1, bottom: -1 }))
    if (m.painted > best.painted) best = m
  }
  console.log(`${label.padEnd(32)} bestes painted=${String(best.painted).padStart(7)}  Band ${best.top}-${best.bottom}`)
  await page.screenshot({ path: `/tmp/sb_${++n}_${label.replace(/\W+/g, '_')}.png` }).catch(() => {})
  // wieder ganz nach unten
  for (let i = 0; i < schritte + 20; i++) await page.mouse.wheel(0, 400)
  await page.waitForTimeout(500)
  return best
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
    }, null, { timeout: 300000 })
    .then(() => true)
    .catch(() => false)
  console.log('Bild erschienen:', erschienen)
  if (!erschienen) { await browser.close(); process.exit(1) }
  await notiere('0_frisch', 2500)

  /* Bild weit nach oben schieben, ohne das Modell zu bemuehen: /help ist ~180
   * Zeilen hoch und wird danach wieder geschlossen. Mehrfach. */
  for (let i = 0; i < 6; i++) {
    await page.keyboard.type('/help')
    await page.waitForTimeout(900)
    await page.keyboard.press('Enter')
    await page.waitForTimeout(1200)
    await page.keyboard.press('Escape')
    await page.waitForTimeout(600)
  }
  await notiere('1_unten_nach_help', 2000)
  await suchHoch('2_hochgescrollt_vor_reload')

  await page.reload()
  await page.waitForSelector('.pane .xterm', { timeout: 30000 })
  await notiere('3_unten_nach_reload', 6000)
  await suchHoch('4_hochgescrollt_nach_reload')
} catch (err) {
  await page.screenshot({ path: '/tmp/sb_fehler.png' }).catch(() => {})
  console.log('FEHLER:', String(err).slice(0, 400))
} finally {
  await browser.close()
}

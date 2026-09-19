/* Welche Ereignisse loeschen das bereits gezeigte Bild — und kommt es zurueck?
 *
 * Erster Lauf (image_persistence_probe) zeigte: Tippen, mehrzeiliger Composer,
 * kurzes Scrollen und zwei weitere Antworten lassen es stehen; ein RESIZE macht
 * es weg. Diese Sonde haertet das ab: weit hinausscrollen und zurueck, Resize und
 * Resize-zurueck, Fenster-Fokuswechsel.
 *
 *   node scripts/e2e/image_loss_events_probe.mjs "http://127.0.0.1:91NN/?token=..."
 */
import { chromium } from 'playwright'

const URL = process.argv[2]
if (!URL) {
  console.error('Aufruf: node scripts/e2e/image_loss_events_probe.mjs <pane-url>')
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
  await page.screenshot({ path: `/tmp/loss_${++n}_${label.replace(/\W+/g, '_')}.png` }).catch(() => {})
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
    await page.screenshot({ path: '/tmp/loss_kein_bild.png' })
    console.log('ABBRUCH: unschluessig.')
    await browser.close()
    process.exit(1)
  }
  await notiere('0_frisch', 2500)

  // Weit hinausscrollen (ueber die Virtualisierungsgrenze) und zurueck.
  await page.mouse.move(550, 400)
  for (let i = 0; i < 12; i++) await page.mouse.wheel(0, -400)
  await notiere('1_weit_hoch')
  for (let i = 0; i < 20; i++) await page.mouse.wheel(0, 400)
  await notiere('2_wieder_unten')

  // Resize und zurueck: kommt das Bild von allein wieder?
  await page.setViewportSize({ width: 900, height: 800 })
  await notiere('3_resize_schmaler', 2500)
  await page.setViewportSize({ width: 1100, height: 800 })
  await notiere('4_resize_zurueck', 2500)

  // Tippen nach dem Resize erzwingt Repaints — holt das es zurueck?
  await page.keyboard.type('abc')
  await notiere('5_tippen_nach_resize')
  for (let i = 0; i < 3; i++) await page.keyboard.press('Backspace')
  await notiere('6_leer_nach_resize')
} catch (err) {
  await page.screenshot({ path: '/tmp/loss_fehler.png' }).catch(() => {})
  console.log('FEHLER:', String(err).slice(0, 300))
} finally {
  await browser.close()
}

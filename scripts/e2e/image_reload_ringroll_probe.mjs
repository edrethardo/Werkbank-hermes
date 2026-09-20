/* Hypothese: im i3-Pane ueberlebt ein Bild den Reload NUR, solange seine
 * kitty-Bytes noch im 512-KiB-Scrollback-Ring liegen (der Server spielt den Ring
 * wortwoertlich zurueck). Ink erfaehrt vom neuen Client nichts, markiert also
 * keinen Block dirty und malt nichts nach.
 *
 * Diese Sonde schiebt den Ring nach dem Bild mit Tastatur-Repaints voll und
 * laedt dann neu.
 *
 *   node scripts/e2e/image_reload_ringroll_probe.mjs "http://127.0.0.1:91NN/?token=..."
 */
import { chromium } from 'playwright'

const URL = process.argv[2]
if (!URL) {
  console.error('Aufruf: node scripts/e2e/image_reload_ringroll_probe.mjs <pane-url>')
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
const notiere = async (label, ms = 2000) => {
  await page.waitForTimeout(ms)
  const m = await measure(page).catch(() => ({ painted: -1, top: -1, bottom: -1 }))
  console.log(`${label.padEnd(30)} painted=${String(m.painted).padStart(7)}  Band ${m.top}-${m.bottom}`)
  await page.screenshot({ path: `/tmp/ring_${++n}_${label.replace(/\W+/g, '_')}.png` }).catch(() => {})
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
    }, null, { timeout: 300000 })
    .then(() => true)
    .catch(() => false)
  console.log('Bild erschienen:', erschienen)
  if (!erschienen) { await browser.close(); process.exit(1) }
  await notiere('0_frisch', 2500)

  /* Ring vollschieben: jeder Tastendruck erzeugt einen Frame-Diff. Zwischendurch
   * messen, damit sichtbar ist, dass das Bild waehrenddessen auf dem Schirm
   * bleibt — es geht nur um den Ring, nicht um den Bildspeicher. */
  for (let runde = 0; runde < 12; runde++) {
    for (let i = 0; i < 200; i++) {
      await page.keyboard.type('x')
    }
    for (let i = 0; i < 200; i++) await page.keyboard.press('Backspace')
    if (runde % 4 === 3) await notiere(`1_fuellen_${runde}`, 300)
  }
  await notiere('2_vor_reload', 1500)

  await page.reload()
  await page.waitForSelector('.pane .xterm', { timeout: 30000 })
  await notiere('3_nach_reload', 6000)

  await page.keyboard.type('abc')
  await notiere('4_tippen', 2500)
  for (let i = 0; i < 3; i++) await page.keyboard.press('Backspace')
  await notiere('5_leer', 2000)
} catch (err) {
  await page.screenshot({ path: '/tmp/ring_fehler.png' }).catch(() => {})
  console.log('FEHLER:', String(err).slice(0, 400))
} finally {
  await browser.close()
}

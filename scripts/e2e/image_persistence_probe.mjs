/* Bleibt das Bild stehen, NACHDEM es einmal erschienen ist?
 *
 * Symptom des Nutzers: "es wird einmal gezeigt und dann geht es weg". Der Block,
 * in den `writeIntoFrame` malt, gehoert dem Frame — aber Inks Zusicherung endet
 * dort, wo der Block sich BEWEGT: neue Zeilen im Transcript, ein Repaint nach
 * Tippen, Scrollen, Resize. Diese Sonde misst genau diese Ereignisse einzeln.
 *
 * Kein Modellturn im Messpfad ausser dem einen, der das Bild ausloest.
 *
 *   node scripts/e2e/image_persistence_probe.mjs "http://127.0.0.1:91NN/?token=..."
 */
import { chromium } from 'playwright'

const URL = process.argv[2]
if (!URL) {
  console.error('Aufruf: node scripts/e2e/image_persistence_probe.mjs <pane-url>')
  process.exit(2)
}

const measure = page =>
  page.evaluate(() => {
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

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1100, height: 800 } })
const schritte = []
const notiere = async (label, ms = 1200) => {
  await page.waitForTimeout(ms)
  const m = await measure(page)
  schritte.push([label, m])
  console.log(`${label.padEnd(34)} painted=${String(m.painted).padStart(7)}  Band ${m.top}-${m.bottom}`)
  await page.screenshot({ path: `/tmp/pers_${schritte.length}_${label.replace(/\W+/g, '_')}.png` }).catch(() => {})
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
    await page.screenshot({ path: '/tmp/pers_kein_bild.png' })
    console.log('ABBRUCH: Ausloeser hat kein Bild erzeugt — Lauf unschluessig. /tmp/pers_kein_bild.png')
    await browser.close()
    process.exit(1)
  }

  await notiere('0_frisch', 2500)

  // 1. Tippen im Composer: Repaint, Frame waechst um keine Zeile.
  await page.keyboard.type('hallo')
  await notiere('1_tippen')

  // 2. Composer leeren (Repaint zurueck).
  for (let i = 0; i < 5; i++) await page.keyboard.press('Backspace')
  await notiere('2_composer_leer')

  // 3. Mehrzeiliger Composer: der Block RUTSCHT nach oben.
  await page.keyboard.type('x'.repeat(400))
  await notiere('3_composer_mehrzeilig')
  for (let i = 0; i < 400; i++) await page.keyboard.press('Backspace')
  await notiere('4_wieder_leer', 2000)

  // 5. Scrollen im Transcript.
  await page.mouse.move(550, 400)
  await page.mouse.wheel(0, -600)
  await notiere('5_hochgescrollt')
  await page.mouse.wheel(0, 900)
  await notiere('6_zurueckgescrollt')

  // 7. Resize (Frame neu vermessen).
  await page.setViewportSize({ width: 900, height: 800 })
  await notiere('7_resize_schmaler', 2500)
} catch (err) {
  await page.screenshot({ path: '/tmp/pers_fehler.png' }).catch(() => {})
  console.log('FEHLER:', String(err).slice(0, 300))
} finally {
  await browser.close()
}

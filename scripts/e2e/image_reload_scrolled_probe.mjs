/* Wie Probe 1, aber naeher an Aarons Lage: das Bild ist beim Reload nicht mehr
 * die letzte Zeile, sondern durch weitere Antworten nach oben gewandert.
 *
 *   node scripts/e2e/image_reload_scrolled_probe.mjs "http://127.0.0.1:91NN/?token=..."
 */
import { chromium } from 'playwright'

const URL = process.argv[2]
if (!URL) {
  console.error('Aufruf: node scripts/e2e/image_reload_scrolled_probe.mjs <pane-url>')
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

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1100, height: 800 } })
let n = 0
const notiere = async (label, ms = 2000) => {
  await page.waitForTimeout(ms)
  const m = await measure(page).catch(() => ({ painted: -1, top: -1, bottom: -1, canvases: -1 }))
  console.log(`${label.padEnd(30)} painted=${String(m.painted).padStart(7)}  Band ${m.top}-${m.bottom}  canvas=${m.canvases}`)
  await page.screenshot({ path: `/tmp/rls_${++n}_${label.replace(/\W+/g, '_')}.png` }).catch(() => {})
  return m
}

const bildDa = () =>
  page.waitForFunction(() => {
    for (const c of document.querySelectorAll('canvas')) {
      if (!/image/i.test(c.className) || !c.width) continue
      const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data
      for (let i = 3; i < d.length; i += 4) if (d[i] > 8) return true
    }
    return false
  }, null, { timeout: 300000 }).then(() => true).catch(() => false)

const sende = async text => {
  await page.keyboard.type(text)
  await page.waitForTimeout(1500)
  await page.keyboard.press('Enter')
}

try {
  await page.goto(URL)
  await page.waitForSelector('.pane .xterm', { timeout: 30000 })
  await page.waitForTimeout(4000)

  await sende('zeig mir MEDIA:/tmp/kitty_test.png')
  console.log('Bild erschienen:', await bildDa())
  await notiere('0_frisch', 2500)

  // Zwei weitere Antworten: das Bild wandert nach oben, bleibt aber auf dem Schirm.
  await sende('antworte nur mit dem wort eins, keine tools')
  await page.waitForTimeout(45000)
  await notiere('1_nach_turn', 1000)
  await sende('antworte nur mit dem wort zwei, keine tools')
  await page.waitForTimeout(45000)
  await notiere('2_nach_turn', 1000)

  await page.reload()
  await page.waitForSelector('.pane .xterm', { timeout: 30000 })
  await notiere('3_nach_reload', 6000)

  await page.keyboard.type('abc')
  await notiere('4_tippen', 2500)
  for (let i = 0; i < 3; i++) await page.keyboard.press('Backspace')
  await notiere('5_leer', 2000)
} catch (err) {
  await page.screenshot({ path: '/tmp/rls_fehler.png' }).catch(() => {})
  console.log('FEHLER:', String(err).slice(0, 400))
} finally {
  await browser.close()
}

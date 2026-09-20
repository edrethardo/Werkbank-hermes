/* Isolierte Messung: Bild steht sichtbar auf dem Schirm, der Replay-Ring ist
 * weitergerollt, dann Reload.
 *
 * Zwei Sackgassen davor, die hier vermieden werden:
 *  - Tastendruecke fuellen 512 KiB nicht (gemessen: 98 KB nach 14 Runden), der
 *    Lauf war gruen ohne den Fehler je zu erzeugen.
 *  - /help fuellt den Ring, scrollt das Bild aber vom Schirm — painted=0 schon
 *    VOR dem Reload, also unschluessig statt rot.
 * Deshalb wird hier der Ring server-seitig klein gestellt (_RING_BYTES) statt
 * ihn vollzuschreiben: das Bild bleibt, wo es ist, nur der Replay ist leer.
 *
 *   ENGINE=webkit node scripts/e2e/image_reload_small_ring.mjs "http://127.0.0.1:9161/?token=..."
 */
import { chromium, webkit } from 'playwright'

const ENGINE = process.env.ENGINE === 'webkit' ? webkit : chromium
const URL = process.argv[2]
if (!URL) {
  console.error('Aufruf: [ENGINE=webkit] node scripts/e2e/image_reload_small_ring.mjs <pane-url>')
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

const tag = process.env.ENGINE || 'chromium'
const browser = await ENGINE.launch()
const page = await browser.newPage({ viewport: { width: 1100, height: 800 } })
let n = 0
const notiere = async (label, ms = 2000) => {
  await page.waitForTimeout(ms)
  const m = await measure(page).catch(() => ({ painted: -1, top: -1, bottom: -1 }))
  console.log(`${label.padEnd(32)} painted=${String(m.painted).padStart(7)}  Band ${m.top}-${m.bottom}`)
  await page.screenshot({ path: `/tmp/sr_${tag}_${++n}_${label.replace(/\W+/g, '_')}.png` }).catch(() => {})
  return m
}

try {
  await page.goto(URL)
  await page.waitForSelector('.pane .xterm', { timeout: 30000 })
  await page.waitForTimeout(5000)

  const PROMPT = 'zeig mir MEDIA:/tmp/kitty_test.png'
  await page.keyboard.type(PROMPT, { delay: 80 })
  await page.waitForTimeout(1500)
  const sichtbar = await page.evaluate(() => document.querySelector('.pane .xterm')?.innerText ?? '')
  if (!sichtbar.includes('/tmp/kitty_test.png')) {
    console.log('ABBRUCH: Eingabe verstuemmelt — unschluessig.')
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
    await page.screenshot({ path: `/tmp/sr_${tag}_kein_bild.png` })
    console.log('ABBRUCH: unschluessig, kein Bild.')
    await browser.close()
    process.exit(1)
  }

  const vorher = await notiere('1_vor_reload', 3000)
  if (vorher.painted <= 0) {
    console.log('ABBRUCH: Bild vor dem Reload nicht auf dem Schirm — unschluessig.')
    await browser.close()
    process.exit(1)
  }

  await page.reload()
  await page.waitForSelector('.pane .xterm', { timeout: 30000 })
  const nachher = await notiere('2_nach_reload', 9000)

  console.log('')
  console.log(nachher.painted > vorher.painted * 0.8
    ? `GRUEN: Bild nach Reload wieder da (${nachher.painted} von ${vorher.painted}).`
    : `ROT: Bild nach Reload weg (${nachher.painted} statt ${vorher.painted}).`)
} catch (err) {
  await page.screenshot({ path: `/tmp/sr_${tag}_fehler.png` }).catch(() => {})
  console.log('FEHLER:', String(err).slice(0, 400))
} finally {
  await browser.close()
}

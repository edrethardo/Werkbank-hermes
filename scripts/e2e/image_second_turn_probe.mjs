/* Der gemeldete Fall: ZWEI Bilder nacheinander in EINER Sitzung, Telefonformat.
 *
 * Gemeldet: das erste Bild kam, das zweite nicht — und nach einem Reload war auch
 * das erste weg. Diese Sonde misst nach jedem Schritt Pixelzahl UND Zeilenband,
 * damit "weg" von "korrekt weggescrollt" unterscheidbar bleibt.
 *
 *   node scripts/e2e/image_second_turn_probe.mjs "http://127.0.0.1:91NN/?token=..."
 *   ENGINE=webkit VIEW=phone node scripts/e2e/image_second_turn_probe.mjs <url>
 *
 * Fallstricke, die hier eingebaut sind (s. Skill hermes-tui-ink-development):
 *  - langsam tippen und den Composer vor Enter pruefen (WebKit verschluckt Zeichen),
 *  - Text und Enter GETRENNT senden,
 *  - vor jedem Ereignis pruefen, dass das Bild ueberhaupt da war.
 */
import { chromium, webkit } from 'playwright'

const URL = process.argv[2]
if (!URL) {
  console.error('Aufruf: node scripts/e2e/image_second_turn_probe.mjs <pane-url>')
  process.exit(2)
}

const ENGINE = process.env.ENGINE === 'webkit' ? webkit : chromium
const VIEW = process.env.VIEW === 'phone' ? { width: 390, height: 700 } : { width: 1100, height: 800 }

const measure = page =>
  page.evaluate(() => {
    let painted = 0, top = -1, bottom = -1, layers = 0
    for (const c of document.querySelectorAll('canvas')) {
      if (!/image/i.test(c.className) || !c.width) continue
      layers++
      const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data
      for (let y = 0; y < c.height; y++) {
        let rowHit = false
        for (let x = 0; x < c.width; x++) {
          if (d[(y * c.width + x) * 4 + 3] > 8) { painted++; rowHit = true }
        }
        if (rowHit) { if (top < 0) top = y; bottom = y }
      }
    }
    return { bottom, layers, painted, top }
  })

const browser = await ENGINE.launch()
const page = await browser.newPage({ viewport: VIEW })
let n = 0

const notiere = async (label, ms = 1500) => {
  await page.waitForTimeout(ms)
  const m = await measure(page)
  console.log(`${label.padEnd(34)} painted=${String(m.painted).padStart(7)}  Band ${m.top}-${m.bottom}  layers=${m.layers}`)
  await page.screenshot({ path: `/tmp/sec_${++n}_${label.replace(/\W+/g, '_')}.png` }).catch(() => {})
  return m
}

/* Tippen mit Kontrolle: WebKit verschluckt ohne `delay` Zeichen, und ein
 * verstuemmelter Prompt liest sich hinterher wie ein Fehler im Renderer. */
const sende = async text => {
  await page.keyboard.type(text, { delay: 60 })
  await page.waitForTimeout(1200)
  const sichtbar = await page.evaluate(() => document.querySelector('.pane .xterm')?.innerText ?? '')
  if (!sichtbar.includes(text.slice(-24))) {
    console.log(`UNSCHLUESSIG: Composer traegt den Prompt nicht (erwartet "…${text.slice(-24)}")`)
    await page.screenshot({ path: `/tmp/sec_tippfehler.png` }).catch(() => {})
    process.exit(3)
  }
  await page.keyboard.press('Enter')
}

const warteAufBild = async (mindestens, timeout = 240000) =>
  page
    .waitForFunction(min => {
      let painted = 0
      for (const c of document.querySelectorAll('canvas')) {
        if (!/image/i.test(c.className) || !c.width) continue
        const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data
        for (let i = 3; i < d.length; i += 4) if (d[i] > 8) painted++
      }
      return painted > min
    }, mindestens, { timeout })
    .then(() => true)
    .catch(() => false)

try {
  await page.goto(URL)
  await page.waitForSelector('.pane .xterm', { timeout: 30000 })
  await page.waitForTimeout(4000)
  console.log(`Engine=${process.env.ENGINE || 'chromium'}  Viewport=${VIEW.width}x${VIEW.height}`)

  await notiere('0_leer', 500)

  await sende('zeig mir MEDIA:/tmp/kitty_test.png')
  const eins = await warteAufBild(0)
  const m1 = await notiere('1_erstes_bild', 2500)
  if (!eins || m1.painted <= 0) {
    console.log('UNSCHLUESSIG: schon das ERSTE Bild kam nicht — alles Weitere misst nichts.')
    process.exit(4)
  }

  await sende('zeig mir MEDIA:/tmp/kitty_test2.png')
  const zwei = await warteAufBild(m1.painted)
  const m2 = await notiere('2_zweites_bild', 2500)
  console.log(`   zweites Bild erschienen: ${zwei} (Zuwachs ${m2.painted - m1.painted} px)`)

  await notiere('3_vor_reload', 500)
  await page.reload()
  await page.waitForSelector('.pane .xterm', { timeout: 30000 })
  await notiere('4_nach_reload', 5000)
} catch (e) {
  console.log('FEHLER:', String(e))
  await page.screenshot({ path: '/tmp/sec_fehler.png' }).catch(() => {})
} finally {
  await browser.close()
}

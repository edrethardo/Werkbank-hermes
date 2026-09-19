/* Bestaetigt die Hypothese: writeAbove hat keinen Platz, wenn Inks Frame den
 * ganzen Schirm fuellt — genau die Lage auf einem Telefon.
 *
 * Gleiche Sequenz, gleiche writeAbove-Mechanik, nur die Framehoehe im
 * Verhaeltnis zur Terminalhoehe variiert. Erwartung:
 *   kleiner Frame  -> Bild bleibt stehen (PC, oben im Pane)
 *   Frame == Schirm -> Bild weg (Telefon)
 *
 *   node scripts/e2e/writeabove_frameheight_probe.mjs
 */
import { chromium } from 'playwright'
import { readFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { join, extname } from 'node:path'

const STATIC = '/home/aaron/.hermes/profiles/hermes_dev/plugins/hermes-i3/static'
const TYPES = { '.mjs': 'text/javascript', '.css': 'text/css' }
let ROWS = 30

const html = () => `<!doctype html><html><head><link rel="stylesheet" href="/vendor/xterm.css">
<style>html,body{margin:0;background:#000}#t{width:900px;height:${ROWS * 20}px}</style></head>
<body><div id="t"></div><script type="module">
import { Terminal } from '/vendor/xterm.mjs'
import { ImageAddon } from '/vendor/addon-image.mjs'
const term = new Terminal({ cols: 100, rows: ${ROWS}, allowProposedApi: true })
const addon = new ImageAddon({ sixelSupport: false, iipSupport: true, storageLimit: 24, pixelLimit: 4194304 })
term.loadAddon(addon); term.open(document.getElementById('t'))
window.__term = term; window.__ready = true
</script></body></html>`

const server = createServer((req, res) => {
  if (req.url === '/') { res.writeHead(200, { 'content-type': 'text/html' }); res.end(html()); return }
  try {
    const p = join(STATIC, req.url.split('?')[0])
    res.writeHead(200, { 'content-type': TYPES[extname(p)] ?? 'application/octet-stream' })
    res.end(readFileSync(p))
  } catch { res.writeHead(404); res.end('x') }
})
await new Promise(r => server.listen(0, '127.0.0.1', r))
const port = server.address().port
const seq = readFileSync('/tmp/probe_seq_kitty.txt', 'utf8')
const browser = await chromium.launch()

console.log('Terminal 100x30, Bild 80x24 Zellen. frameRows = Inks Framehoehe.\n')

for (const frameRows of [6, 12, 20, 26, 29]) {
  const page = await browser.newPage({ viewport: { width: 1000, height: ROWS * 20 + 40 } })
  await page.goto(`http://127.0.0.1:${port}/`)
  await page.waitForFunction(() => window.__ready === true, null, { timeout: 15000 })
  const write = s => page.evaluate(s => new Promise(r => window.__term.write(s, r)), s)
  const px = () => page.evaluate(() => {
    let n = 0
    for (const c of document.querySelectorAll('canvas')) {
      if (!/image/i.test(c.className) || !c.width) continue
      const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data
      for (let i = 0; i < d.length; i += 4) if (d[i + 3] > 8) n++
    }
    return n
  })

  // Transcript fuellen, dann einen Frame dieser Hoehe, Cursor in dessen letzter Zeile.
  await write('TRANSCRIPT\r\n'.repeat(3))
  for (let i = 0; i < frameRows - 1; i++) await write(`frame-${i}\r\n`)
  await write('> composer')

  // writeAbove, exakt wie ink.tsx:1391
  await write(`\r${frameRows - 1 > 0 ? `\x1b[${frameRows - 1}A` : ''}\x1b[J${seq}\n`)
  await page.waitForTimeout(700)
  const direkt = await px()

  // Ink zeichnet seinen Frame sofort neu.
  for (let i = 0; i < frameRows - 1; i++) await write(`frame-${i}\r\n`)
  await write('> composer')
  await page.waitForTimeout(700)
  const nachher = await px()

  console.log(`frameRows=${String(frameRows).padStart(2)} (Schirm ${ROWS})  ` +
              `direkt=${String(direkt).padStart(7)}  nach Repaint=${String(nachher).padStart(7)}` +
              (nachher === 0 ? '   <-- WEG' : ''))
  await page.screenshot({ path: `/tmp/fh_${frameRows}.png` })
  await page.close()
}

await browser.close(); server.close()

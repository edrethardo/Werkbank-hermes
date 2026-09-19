/* Die ECHTE writeAbove-Sequenz, nicht nur ihr payload.
 *
 * writeAbove schreibt:  \r  ESC[<frameRows>A  <eraseToEndOfScreen>  <bild>  \n
 * und laesst Ink danach den Frame neu zeichnen. Der Probe davor hat NUR das
 * Bild gefuettert und war gruen — hier kommt der Rahmen drumherum dazu, inkl.
 * des Hochsprungs und des Loeschens.
 *
 *   node scripts/e2e/write_above_consumer_probe.mjs
 */
import { chromium } from 'playwright'
import { readFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { join, extname } from 'node:path'

const STATIC = '/home/aaron/.hermes/profiles/hermes_dev/plugins/hermes-i3/static'
const TYPES = { '.mjs': 'text/javascript', '.css': 'text/css' }
const ERASE_TO_END = '\x1b[J'

const PAGE = `<!doctype html><html><head><link rel="stylesheet" href="/vendor/xterm.css">
<style>html,body{margin:0;background:#000}#t{width:900px;height:620px}</style></head>
<body><div id="t"></div><script type="module">
import { Terminal } from '/vendor/xterm.mjs'
import { ImageAddon } from '/vendor/addon-image.mjs'
const term = new Terminal({ cols: 100, rows: 30, allowProposedApi: true })
const addon = new ImageAddon({ sixelSupport: false, iipSupport: true, storageLimit: 24, pixelLimit: 4194304 })
term.loadAddon(addon); term.open(document.getElementById('t'))
window.__term = term; window.__ready = true
</script></body></html>`

const server = createServer((req, res) => {
  if (req.url === '/') { res.writeHead(200, { 'content-type': 'text/html' }); res.end(PAGE); return }
  try {
    const p = join(STATIC, req.url.split('?')[0])
    res.writeHead(200, { 'content-type': TYPES[extname(p)] ?? 'application/octet-stream' })
    res.end(readFileSync(p))
  } catch { res.writeHead(404); res.end('x') }
})
await new Promise(r => server.listen(0, '127.0.0.1', r))
const port = server.address().port

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1000, height: 700 } })
await page.goto(`http://127.0.0.1:${port}/`)
await page.waitForFunction(() => window.__ready === true, null, { timeout: 15000 })

const seq = readFileSync('/tmp/probe_seq_kitty.txt', 'utf8')
const write = s => page.evaluate(s => new Promise(r => window.__term.write(s, r)), s)
const paintedPx = () => page.evaluate(() => {
  let n = 0
  for (const c of document.querySelectorAll('canvas')) {
    if (!/image/i.test(c.className) || !c.width) continue
    const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data
    for (let i = 0; i < d.length; i += 4) if (d[i + 3] > 8) n++
  }
  return n
})
const cursor = () => page.evaluate(() => ({ x: window.__term.buffer.active.cursorX, y: window.__term.buffer.active.cursorY }))

// Ausgangslage: eine Transcript-Zeile plus ein Ink-Frame von FRAME_ROWS+1 Zeilen,
// Cursor in dessen letzter Zeile — genau die Lage, aus der writeAbove startet.
const FRAME_ROWS = 8
await write('TRANSCRIPT-ZEILE\r\n')
for (let i = 0; i < FRAME_ROWS; i++) await write(`frame-${i}\r\n`)
await write('> composer')
console.log('Start        ', JSON.stringify(await cursor()))

// --- writeAbove, Zeichen fuer Zeichen wie in ink.tsx ---
await write(`\r\x1b[${FRAME_ROWS}A${ERASE_TO_END}${seq}\n`)
await page.waitForTimeout(900)
console.log('nach writeAbove', JSON.stringify(await cursor()), 'pixel=', await paintedPx())
await page.screenshot({ path: '/tmp/wa_1_nach_writeabove.png' })

// Ink zeichnet danach seinen Frame sofort neu (repaint + onRender).
for (let i = 0; i < FRAME_ROWS; i++) await write(`frame-${i}\r\n`)
await write('> composer')
await page.waitForTimeout(800)
console.log('nach repaint ', JSON.stringify(await cursor()), 'pixel=', await paintedPx())
await page.screenshot({ path: '/tmp/wa_2_nach_repaint.png' })

await browser.close(); server.close()

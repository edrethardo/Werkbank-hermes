/* Wo steht der Cursor NACH der kitty-Sequenz — und ueberlebt das Bild einen
 * Frame-Redraw an derselben Stelle?
 *
 * `writeAbove` malt das Bild und laesst Ink danach SOFORT seinen Frame neu
 * zeichnen. Bewegt der kitty-Handler den Cursor nicht unter das Bild, schreibt
 * Ink seinen Text in genau die Zeilen, in denen das Bild liegt.
 *
 *   node scripts/e2e/kitty_cursor_probe.mjs
 */
import { chromium } from 'playwright'
import { readFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { join, extname } from 'node:path'

const STATIC = '/home/aaron/.hermes/profiles/hermes_dev/plugins/hermes-i3/static'
const TYPES = { '.mjs': 'text/javascript', '.css': 'text/css' }

const PAGE = `<!doctype html><html><head><link rel="stylesheet" href="/vendor/xterm.css">
<style>html,body{margin:0;background:#000}#t{width:900px;height:620px}</style></head>
<body><div id="t"></div><script type="module">
import { Terminal } from '/vendor/xterm.mjs'
import { ImageAddon } from '/vendor/addon-image.mjs'
const term = new Terminal({ cols: 100, rows: 30, allowProposedApi: true })
const addon = new ImageAddon({ sixelSupport: false, iipSupport: true, storageLimit: 24, pixelLimit: 4194304 })
term.loadAddon(addon); term.open(document.getElementById('t'))
window.__term = term; window.__addon = addon; window.__ready = true
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

const paintedPx = () => page.evaluate(() => {
  let n = 0
  for (const c of document.querySelectorAll('canvas')) {
    if (!/image/i.test(c.className) || !c.width) continue
    const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data
    for (let i = 0; i < d.length; i += 4) if (d[i + 3] > 8) n++
  }
  return n
})

const write = s => page.evaluate(s => new Promise(r => window.__term.write(s, r)), s)
const cursor = () => page.evaluate(() => ({ x: window.__term.buffer.active.cursorX, y: window.__term.buffer.active.cursorY,
                                            base: window.__term.buffer.active.baseY }))

console.log('vorher      ', JSON.stringify(await cursor()))
await write('ZEILE-EINS\r\n')
console.log('nach Text   ', JSON.stringify(await cursor()))
await write(seq)
await page.waitForTimeout(800)
const afterImg = await cursor()
console.log('nach Bild   ', JSON.stringify(afterImg), 'pixel=', await paintedPx())
await page.screenshot({ path: '/tmp/cursor_1_nach_bild.png' })

// Genau das, was Ink danach tut: Frame neu zeichnen, ab der aktuellen Cursorzeile.
await write('\r\n')
await write('FRAME-ZEILE-A\r\nFRAME-ZEILE-B\r\nFRAME-ZEILE-C\r\n> composer')
await page.waitForTimeout(600)
console.log('nach Frame  ', JSON.stringify(await cursor()), 'pixel=', await paintedPx())
await page.screenshot({ path: '/tmp/cursor_2_nach_frame.png' })

await browser.close(); server.close()

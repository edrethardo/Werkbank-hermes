/* Was macht der kitty-Handler, wenn das Bild BREITER ist als das Terminal?
 *
 * `transcript_images.MAX_COLS` ist fest 80. Auf einem Telefon hat ein i3-Pane
 * gut die Haelfte davon. Diese Sonde faehrt dieselbe Sequenz durch Terminals
 * verschiedener Breite und zaehlt die gemalten Pixel.
 *
 *   node scripts/e2e/narrow_terminal_probe.mjs
 */
import { chromium, webkit } from 'playwright'
import { readFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { join, extname } from 'node:path'

const ENGINE = process.env.ENGINE === 'webkit' ? webkit : chromium
const STATIC = '/home/aaron/.hermes/profiles/hermes_dev/plugins/hermes-i3/static'
const TYPES = { '.mjs': 'text/javascript', '.css': 'text/css' }

const page_html = (cols, rows) => `<!doctype html><html><head>
<link rel="stylesheet" href="/vendor/xterm.css">
<style>html,body{margin:0;background:#000}#t{width:100%;height:100%}</style></head>
<body><div id="t"></div><script type="module">
import { Terminal } from '/vendor/xterm.mjs'
import { ImageAddon } from '/vendor/addon-image.mjs'
const term = new Terminal({ cols: ${cols}, rows: ${rows}, fontSize: 11, allowProposedApi: true })
const addon = new ImageAddon({ sixelSupport: false, iipSupport: true, storageLimit: 24, pixelLimit: 4194304 })
term.loadAddon(addon); term.open(document.getElementById('t'))
window.__term = term; window.__addon = addon; window.__ready = true
</script></body></html>`

let COLS = 80, ROWS = 30
const server = createServer((req, res) => {
  if (req.url === '/') { res.writeHead(200, { 'content-type': 'text/html' }); res.end(page_html(COLS, ROWS)); return }
  try {
    const p = join(STATIC, req.url.split('?')[0])
    res.writeHead(200, { 'content-type': TYPES[extname(p)] ?? 'application/octet-stream' })
    res.end(readFileSync(p))
  } catch { res.writeHead(404); res.end('x') }
})
await new Promise(r => server.listen(0, '127.0.0.1', r))
const port = server.address().port

const seq = readFileSync('/tmp/probe_seq_kitty.txt', 'utf8')
const browser = await ENGINE.launch()

console.log('Bild ist c=80, r=24 Zellen gross.\n')
for (const [cols, rows] of [[100, 30], [80, 30], [60, 30], [45, 30], [40, 24], [32, 20]]) {
  COLS = cols; ROWS = rows
  const page = await browser.newPage({ viewport: { width: 1000, height: 700 } })
  const errs = []
  page.on('pageerror', e => errs.push(String(e)))
  await page.goto(`http://127.0.0.1:${port}/`)
  await page.waitForFunction(() => window.__ready === true, null, { timeout: 15000 })

  const painted = await page.evaluate(async seq => {
    await new Promise(r => window.__term.write('\r\n' + seq + '\r\n', r))
    await new Promise(r => setTimeout(r, 700))
    let n = 0
    for (const c of document.querySelectorAll('canvas')) {
      if (!/image/i.test(c.className) || !c.width) continue
      const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data
      for (let i = 0; i < d.length; i += 4) if (d[i + 3] > 8) n++
    }
    return { n, usage: window.__addon.storageUsage }
  }, seq)

  console.log(`cols=${String(cols).padStart(3)} rows=${String(rows).padStart(2)}  ` +
              `pixel=${String(painted.n).padStart(7)}  storage=${painted.usage.toFixed(3)}` +
              (errs.length ? `  FEHLER: ${errs[0]}` : ''))
  await page.screenshot({ path: `/tmp/narrow_${cols}.png` })
  await page.close()
}

await browser.close(); server.close()

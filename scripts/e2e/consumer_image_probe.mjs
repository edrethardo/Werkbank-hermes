/* Malt der xterm.js des i3-Plugins die ECHTE kitty-Sequenz des Gateways?
 *
 * Stufe 4 der Leiter (references/localising-a-silent-delivery-break.md): Consumer
 * isoliert, ohne Ink, ohne PTY, ohne Modell. Eingabe sind die Bytes, die
 * `tui_gateway.transcript_images.render_image()` wirklich produziert.
 *
 *   node scripts/e2e/consumer_image_probe.mjs
 */
import { chromium, webkit } from 'playwright'
const ENGINE = process.env.ENGINE === 'webkit' ? webkit : chromium
import { readFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { join, extname } from 'node:path'

const STATIC = '/home/aaron/.hermes/profiles/hermes_dev/plugins/hermes-i3/static'
const TYPES = { '.mjs': 'text/javascript', '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html' }

const PAGE = `<!doctype html><html><head>
<link rel="stylesheet" href="/vendor/xterm.css">
<style>html,body{margin:0;background:#000}#t{width:900px;height:600px}</style>
</head><body><div id="t"></div>
<script type="module">
import { Terminal } from '/vendor/xterm.mjs'
import { ImageAddon } from '/vendor/addon-image.mjs'
const term = new Terminal({ cols: 100, rows: 30, allowProposedApi: true })
// EXAKT die Optionen aus app.js.
const addon = new ImageAddon({ sixelSupport: false, iipSupport: true, storageLimit: 24, pixelLimit: 4194304 })
term.loadAddon(addon)
term.open(document.getElementById('t'))
window.__term = term
window.__addon = addon
window.__opts = JSON.parse(JSON.stringify(addon._opts ?? {}))
window.__handlers = [...(addon._handlers?.keys() ?? [])]
window.__ready = true
</script></body></html>`

const server = createServer((req, res) => {
  if (req.url === '/' || req.url.startsWith('/?')) {
    res.writeHead(200, { 'content-type': 'text/html' }); res.end(PAGE); return
  }
  try {
    const p = join(STATIC, req.url.split('?')[0])
    res.writeHead(200, { 'content-type': TYPES[extname(p)] ?? 'application/octet-stream' })
    res.end(readFileSync(p))
  } catch { res.writeHead(404); res.end('nope') }
})

await new Promise(r => server.listen(0, '127.0.0.1', r))
const port = server.address().port

const browser = await ENGINE.launch()
const page = await browser.newPage({ viewport: { width: 1000, height: 700 } })
const errs = []
page.on('pageerror', e => errs.push(String(e)))
page.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text()) })
await page.goto(`http://127.0.0.1:${port}/`)
await page.waitForFunction(() => window.__ready === true, null, { timeout: 15000 })

const setup = await page.evaluate(() => ({ opts: window.__opts, handlers: window.__handlers }))

async function feed(label, file) {
  const seq = readFileSync(file, 'utf8')
  const r = await page.evaluate(async seq => {
    await new Promise(res => window.__term.write('\\r\\n' + seq + '\\r\\n', res))
    await new Promise(res => setTimeout(res, 900))
    const canvases = [...document.querySelectorAll('canvas')]
    const layers = canvases.map(c => ({ cls: c.className, w: c.width, h: c.height }))
    // Nicht-schwarze Pixel auf den Image-Layern zaehlen.
    let painted = 0
    for (const c of canvases) {
      if (!/image/i.test(c.className)) continue
      const ctx = c.getContext('2d')
      if (!ctx || !c.width || !c.height) continue
      const d = ctx.getImageData(0, 0, c.width, c.height).data
      for (let i = 0; i < d.length; i += 4) if (d[i + 3] > 8) painted++
    }
    return { layers, painted, storageUsage: window.__addon.storageUsage }
  }, seq)
  console.log(`\n--- ${label} (${seq.length} bytes) ---`)
  console.log(JSON.stringify(r, null, 2))
  await page.screenshot({ path: `/tmp/consumer_${label}.png` })
  return r
}

console.log('Addon-Setup:', JSON.stringify(setup))
await feed('kitty', '/tmp/probe_seq_kitty.txt')
await page.evaluate(() => new Promise(r => window.__term.reset(), r))
await feed('iterm', '/tmp/probe_seq_iterm.txt')

console.log('\nJS-Fehler:', errs.length ? errs : 'keine')
await browser.close()
server.close()

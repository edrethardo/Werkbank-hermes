// Screen side of scripts/e2e/bg_progress_dock.py: ONE WebSocket to the dashboard's
// /api/pty (exactly what the /chat page opens), bytes fed into a headless xterm.js,
// the visible screen printed as a JSON line every 500 ms.
//
//   node bg_progress_dock_client.mjs <ws-url> <cols> <rows> <prompt> <seconds>
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

globalThis.window = globalThis
globalThis.self = globalThis
globalThis.navigator ??= { userAgent: 'node', platform: 'Linux' }

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const require = createRequire(join(ROOT, 'package.json'))
const WebSocket = require('ws')
const { Terminal } = await import(join(ROOT, 'node_modules', '@xterm', 'xterm', 'lib', 'xterm.mjs'))
const { Unicode11Addon } = await import(join(ROOT, 'node_modules', '@xterm', 'addon-unicode11', 'lib', 'addon-unicode11.mjs'))

const [url, cols, rows, prompt, seconds] = process.argv.slice(2)
const term = new Terminal({ cols: Number(cols), rows: Number(rows), allowProposedApi: true, scrollback: 2000 })
// Same cell widths as the /chat page (ChatPage.tsx loads Unicode 11 too).
term.loadAddon(new Unicode11Addon())
term.unicode.activeVersion = '11'
const sock = new WebSocket(url)
sock.binaryType = 'arraybuffer'

const screen = () => {
  const b = term.buffer.active
  const out = []
  for (let i = 0; i < term.rows; i++) out.push(b.getLine(b.viewportY + i)?.translateToString(true) ?? '')
  return out
}

let typed = false
let bytes = 0
const started = Date.now()
sock.on('open', () => sock.send(`\x1b[RESIZE:${cols};${rows}]`))
sock.on('message', data => {
  const chunk = Buffer.from(data)
  bytes += chunk.length
  term.write(chunk)
  // Type once the composer has painted (the TUI draws its prompt glyph).
  if (!typed && Date.now() - started > 1500 && screen().some(l => /❯|›|>\s*$/.test(l))) {
    typed = true
    setTimeout(() => sock.send(prompt), 300)
    setTimeout(() => sock.send('\r'), 900)
  }
})
sock.on('close', () => console.log(JSON.stringify({ event: 'closed' })))
sock.on('error', e => console.log(JSON.stringify({ event: 'error', message: String(e) })))

const timer = setInterval(() => {
  console.log(JSON.stringify({ t: (Date.now() - started) / 1000, typed, bytes, screen: screen() }))
}, 500)

setTimeout(() => {
  clearInterval(timer)
  console.log(JSON.stringify({ event: 'done', t: (Date.now() - started) / 1000 }))
  sock.close()
  setTimeout(() => process.exit(0), 300)
}, Number(seconds) * 1000)

// WebKit/iPhone side of scripts/e2e/bg_progress_dock.py (BROWSER=webkit): the REAL /chat page
// of the dashboard in Playwright's WebKit with iPhone 13 emulation. Counts the /api/pty sockets
// the page opens, types the prompt, and screenshots the page while the process runs.
//
//   node bg_progress_dock_webkit.mjs <dashboard-url> <prompt> <seconds> <out-dir>
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const require = createRequire(join(ROOT, 'package.json'))
const { devices, webkit } = require('playwright')

const [base, prompt, seconds, out] = process.argv.slice(2)
const browser = await webkit.launch()
const context = await browser.newContext({ ...devices['iPhone 13'] })
const page = await context.newPage()
const ptySockets = []
page.on('websocket', ws => {
  if (ws.url().includes('/api/pty')) ptySockets.push(ws.url().replace(/token=[^&]+/, 'token=…'))
})
const log = obj => console.log(JSON.stringify(obj))

await page.goto(`${base}/chat`, { waitUntil: 'domcontentloaded' })
const started = Date.now()
// Wait for the TUI to paint its composer, then type like a user.
await page.waitForSelector('.xterm', { timeout: 60000 })
await page.waitForTimeout(6000)
await page.locator('.xterm').first().tap().catch(() => {})
await page.keyboard.type(prompt, { delay: 30 })
await page.keyboard.press('Enter')
log({ event: 'typed', t: (Date.now() - started) / 1000 })

let n = 0
const until = Date.now() + Number(seconds) * 1000
while (Date.now() < until) {
  await page.waitForTimeout(2000)
  const path = join(out, `webkit-${String(n++).padStart(2, '0')}.png`)
  await page.screenshot({ path })
  log({ event: 'shot', t: (Date.now() - started) / 1000, path })
}
log({ event: 'done', ptySockets })
await browser.close()

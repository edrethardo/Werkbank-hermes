import { afterEach, describe, expect, it, vi } from 'vitest'

/* IMAGE_PROTOCOL loest beim Modulladen auf, also wird das Modul je Fall frisch geladen. */
const load = async (env: Record<string, string | undefined>) => {
  vi.resetModules()

  const saved: Record<string, string | undefined> = {}

  for (const [key, value] of Object.entries(env)) {
    saved[key] = process.env[key]

    if (value === undefined) {
      delete process.env[key]
    } else {
      process.env[key] = value
    }
  }

  try {
    return await import('../config/env.js')
  } finally {
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) {
        delete process.env[key]
      } else {
        process.env[key] = value
      }
    }
  }
}

afterEach(() => {
  vi.resetModules()
})

describe('IMAGE_PROTOCOL', () => {
  it('is empty on a plain terminal so the gateway keeps deciding', async () => {
    // Ohne Angabe muss die Erkennung im Gateway greifen — ein hier geratenes
    // Protokoll waere fuer jedes echte Terminal schlechter als dessen TERM-Auswertung.
    const env = await load({ HERMES_TUI_DASHBOARD: undefined, HERMES_TUI_IMAGE_PROTOCOL: undefined })

    expect(env.IMAGE_PROTOCOL).toBe('')
  })

  it('picks iterm in the dashboard, where the terminal is xterm.js', async () => {
    // Der Kern: das Gateway liest TERM des Dashboard-Prozesses und sieht nie das
    // Browser-Terminal. xterm.js kann kitty-Grafik nicht parsen, IIP schon.
    const env = await load({ HERMES_TUI_DASHBOARD: '1', HERMES_TUI_IMAGE_PROTOCOL: undefined })

    expect(env.IMAGE_PROTOCOL).toBe('iterm')
  })

  it('lets an embedder that knows its terminal keep kitty inside the dashboard', async () => {
    // i3-Panes tragen BEIDE Variablen (sie erben das Dashboard-Env) und liefern ein
    // selbstgebautes xterm.js MIT kitty-Handler aus. Gaebe der Dashboard-Fall den
    // Ausschlag, bekaemen sie iterm und damit ein Protokoll, das ihr Terminal nicht malt.
    const env = await load({ HERMES_TUI_DASHBOARD: '1', HERMES_TUI_IMAGE_PROTOCOL: 'kitty' })

    expect(env.IMAGE_PROTOCOL).toBe('kitty')
  })

  it('honours an explicit protocol outside the dashboard', async () => {
    const env = await load({ HERMES_TUI_DASHBOARD: undefined, HERMES_TUI_IMAGE_PROTOCOL: 'kitty' })

    expect(env.IMAGE_PROTOCOL).toBe('kitty')
  })

  it('ignores a value the gateway would reject', async () => {
    // Ein Tippfehler darf nicht als Protokoll durchgereicht werden; dann entscheidet
    // wieder das Gateway.
    const env = await load({ HERMES_TUI_DASHBOARD: undefined, HERMES_TUI_IMAGE_PROTOCOL: 'sixel' })

    expect(env.IMAGE_PROTOCOL).toBe('')
  })
})

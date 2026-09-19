/* `writeIntoFrame`: malt Rohbytes in einen Block, den der Frame reserviert hat.
 *
 * Der Unterschied zu `writeAbove` ist der ganze Punkt. `writeAbove` schreibt
 * OBERHALB des Frames; der Repaint zeichnet den Frame von dort abwaerts neu und
 * frisst die Bytes anteilig zur Framehoehe wieder auf (live gemessen auf einem
 * 30-Zeilen-Schirm: bei 6 Frame-Zeilen blieb ein kitty-Bild ganz, bei 29 noch
 * 4 %). Und solange die Sitzung auf einen Schirm passt, beginnt der Frame beim
 * Banner — "oberhalb" ist dann Zeile 0, weit weg vom zugehoerigen Text.
 *
 * Hier gehoeren die Zellen dem Frame. Der Marker sagt, WELCHE Zeile es ist: der
 * Abstand des Blocks zum Cursor haengt von allem darunter ab (Composer,
 * Aktivitaetszeile, Prompts), was kein Aufrufer ausrechnen kann.
 */
import { EventEmitter } from 'events'

import React from 'react'
import { describe, expect, it } from 'vitest'

import Box from './components/Box.js'
import Text from './components/Text.js'
import Ink from './ink.js'

class FakeTty extends EventEmitter {
  chunks: string[] = []
  columns = 40
  rows = 12
  isTTY = true

  write(chunk: string | Uint8Array, cb?: (err?: Error | null) => void): boolean {
    this.chunks.push(typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8'))
    cb?.()

    return true
  }
}

function makeInk() {
  const stdout = new FakeTty()

  const ink = new Ink({
    exitOnCtrlC: false,
    patchConsole: false,
    stderr: new FakeTty() as unknown as NodeJS.WriteStream,
    stdin: new FakeTty() as unknown as NodeJS.ReadStream,
    stdout: stdout as unknown as NodeJS.WriteStream
  })

  return { ink, stdout }
}

const MARKER = '\u2800\u2800img0\u2800'
const PAYLOAD = '<<PIXELS>>'

/** A transcript, a marked one-row block, then more rows below it. */
const tree = (marker = MARKER) =>
  React.createElement(
    Box,
    { flexDirection: 'column' },
    React.createElement(Text, { key: 'a' }, 'transcript row'),
    React.createElement(Box, { flexDirection: 'column', height: 1, key: 'b' }, React.createElement(Text, null, marker)),
    React.createElement(Text, { key: 'c' }, 'answer text'),
    React.createElement(Text, { key: 'd' }, '> composer')
  )

describe('writeIntoFrame', () => {
  it('moves up to the marked row and brackets the write with cursor save/restore', () => {
    const { ink, stdout } = makeInk()
    ink.render(tree())
    ink.onRender()
    stdout.chunks.length = 0

    expect(ink.writeIntoFrame(MARKER, PAYLOAD)).toBe(true)

    const written = stdout.chunks.join('')
    expect(written).toContain(PAYLOAD)
    // Gesichert und wiederhergestellt: Ink findet den Cursor genau dort wieder,
    // wo es ihn geparkt hat — sonst rechnet sein naechster relativer Sprung falsch.
    expect(written.startsWith('\u001b[s')).toBe(true)
    expect(written.endsWith('\u001b[u')).toBe(true)

    // Der Block liegt oberhalb des Cursors, also MUSS hochgefahren werden. Die
    // Zeilenzahl wird nicht festgenagelt (das waere ein Change-Detector), aber
    // sie muss im Schirm liegen und vor der Nutzlast stehen.
    const up = /\u001b\[(\d+)A/.exec(written)
    expect(up).not.toBeNull()
    expect(Number(up![1])).toBeGreaterThan(0)
    expect(Number(up![1])).toBeLessThan(stdout.rows)
    expect(written.indexOf(up![0])).toBeLessThan(written.indexOf(PAYLOAD))
  })

  it('refuses when the marker is not in the frame instead of painting blind', () => {
    /* Ohne diese Absage landete die Nutzlast irgendwo — der Aufrufer muss auf den
     * Render warten koennen, und "nicht gemalt" ist ein brauchbares Signal. */
    const { ink } = makeInk()
    ink.render(tree())
    ink.onRender()

    expect(ink.writeIntoFrame('\u2800\u2800nicht-da\u2800', PAYLOAD)).toBe(false)
  })

  it('paints each marker into its own row', () => {
    /* Zwei Bilder in einer Antwort: gleicher Marker hiesse, beide landen im Block
     * des ersten, weil die ERSTE Trefferzeile gewinnt. */
    const { ink, stdout } = makeInk()
    const second = '\u2800\u2800img1\u2800'

    ink.render(
      React.createElement(
        Box,
        { flexDirection: 'column' },
        React.createElement(Box, { height: 1, key: 'a' }, React.createElement(Text, null, MARKER)),
        React.createElement(Text, { key: 'x' }, 'zwischen'),
        React.createElement(Box, { height: 1, key: 'b' }, React.createElement(Text, null, second)),
        React.createElement(Text, { key: 'c' }, '> composer')
      )
    )
    ink.onRender()

    stdout.chunks.length = 0
    expect(ink.writeIntoFrame(MARKER, 'ERSTES')).toBe(true)
    const ersterSprung = Number(/\u001b\[(\d+)A/.exec(stdout.chunks.join(''))![1])

    stdout.chunks.length = 0
    expect(ink.writeIntoFrame(second, 'ZWEITES')).toBe(true)
    const zweiterSprung = Number(/\u001b\[(\d+)A/.exec(stdout.chunks.join(''))![1])

    // Der zweite Block liegt weiter unten, also ist sein Sprung kuerzer.
    expect(zweiterSprung).toBeLessThan(ersterSprung)
  })

  it('declines on a non-TTY rather than emitting escapes into a pipe', () => {
    const { ink, stdout } = makeInk()
    ink.render(tree())
    ink.onRender()
    stdout.isTTY = false

    expect(ink.writeIntoFrame(MARKER, PAYLOAD)).toBe(false)
  })
})

/* Einmal malen reicht nicht. Die Pixel gehoeren dem Terminal, nicht Inks
 * Zellmodell — was den Schirm ueber diesen Zeilen neu zeichnet (Overlay,
 * ctrl+L, Resize), loescht sie, waehrend Ink den (leeren) Block fuer aktuell
 * haelt und keinen Diff dafuer erzeugt. Live gemessen: `/help`, ctrl+L und ein
 * Resize machten ein kitty-Bild weg, und es kam NIE zurueck. */
describe('writeIntoFrame: das Bild ueberlebt Repaints', () => {
  /** Ein Overlay, das die Zeilen unter dem Marker belegt. */
  const overlay = () =>
    React.createElement(
      Box,
      { flexDirection: 'column' },
      React.createElement(Text, { key: 'a' }, 'transcript row'),
      React.createElement(Text, { key: 'o1' }, 'OVERLAY ZEILE 1'),
      React.createElement(Text, { key: 'o2' }, 'OVERLAY ZEILE 2'),
      React.createElement(Text, { key: 'd' }, '> composer')
    )

  /** Block mit mehreren Zeilen: nur die erste traegt den Marker. */
  const tallTree = (extra: null | string = null) =>
    React.createElement(
      Box,
      { flexDirection: 'column' },
      React.createElement(Text, { key: 'a' }, 'transcript row'),
      React.createElement(
        Box,
        { flexDirection: 'column', height: 3, key: 'b' },
        React.createElement(Text, null, MARKER),
        extra === null ? null : React.createElement(Text, null, extra)
      ),
      React.createElement(Text, { key: 'd' }, '> composer')
    )

  it('repaints after a frame that erased the screen', () => {
    const { ink, stdout } = makeInk()
    ink.render(tree())
    ink.onRender()
    expect(ink.writeIntoFrame(MARKER, PAYLOAD)).toBe(true)

    // Genau das macht ctrl+L: Schirm loeschen und alles neu zeichnen. Die
    // Bildbytes sind damit weg — Ink muss sie von sich aus nachmalen.
    stdout.chunks.length = 0
    ink.forceRedraw()

    expect(stdout.chunks.join('')).toContain(PAYLOAD)
  })

  it('repaints once the overlay covering the block is gone', () => {
    const { ink, stdout } = makeInk()
    ink.render(tallTree())
    ink.onRender()
    expect(ink.writeIntoFrame(MARKER, PAYLOAD, 3)).toBe(true)

    // Waehrend das Overlay steht, darf NICHT gemalt werden — das Bild landete
    // sonst ueber fremdem Text.
    stdout.chunks.length = 0
    ink.render(overlay())
    ink.onRender()
    expect(stdout.chunks.join('')).not.toContain(PAYLOAD)

    // Overlay zu: die Zeilen gehoeren wieder dem Block, jetzt muss es kommen.
    stdout.chunks.length = 0
    ink.render(tallTree())
    ink.onRender()
    expect(stdout.chunks.join('')).toContain(PAYLOAD)
  })

  it('holds back while foreign content sits in the block rows', () => {
    /* Der Marker allein genuegt nicht: ein Overlay, das nur die Zeilen UNTER
     * ihm belegt, sieht sonst aus wie ein unberuehrter Block. Deshalb reist die
     * Blockhoehe mit. */
    const { ink, stdout } = makeInk()
    ink.render(tallTree())
    ink.onRender()
    expect(ink.writeIntoFrame(MARKER, PAYLOAD, 3)).toBe(true)

    stdout.chunks.length = 0
    ink.render(tallTree('FREMDER TEXT'))
    ink.onRender()

    expect(stdout.chunks.join('')).not.toContain(PAYLOAD)
  })

  it('forgets a cleared block instead of painting it into later content', () => {
    const { ink, stdout } = makeInk()
    ink.render(tree())
    ink.onRender()
    ink.writeIntoFrame(MARKER, PAYLOAD)

    ink.clearFrameBlock(MARKER)
    stdout.chunks.length = 0
    ink.forceRedraw()

    expect(stdout.chunks.join('')).not.toContain(PAYLOAD)
  })

  it('declines a block scrolled out of the visible screen', () => {
    /* Ein Block weit oben im Scrollback ist mit relativen Cursorbewegungen
     * nicht erreichbar; dort zu malen landet irgendwo auf dem Schirm. Live
     * gemessen umfasst der Frame das ganze Transcript (222 Zeilen auf einem
     * 52-Zeilen-Terminal), die Framehoehe taugt also nicht als Grenze. */
    const { ink, stdout } = makeInk()
    const kinder = [React.createElement(Box, { height: 1, key: 'b' }, React.createElement(Text, null, MARKER))]

    for (let i = 0; i < stdout.rows * 3; i++) {
      kinder.push(React.createElement(Text, { key: `f${i}` }, `zeile ${i}`))
    }

    ink.render(React.createElement(Box, { flexDirection: 'column' }, ...kinder))
    ink.onRender()

    expect(ink.writeIntoFrame(MARKER, PAYLOAD)).toBe(false)
  })
})

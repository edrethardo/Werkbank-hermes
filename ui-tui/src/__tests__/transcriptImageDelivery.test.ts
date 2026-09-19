/* Auslieferung der `MEDIA:`-Bilder im Transcript.
 *
 * Gepinnt werden drei Verhalten, jedes aus einem echten Fehler entstanden:
 *
 *  1. Ein Bild wird pro Antwort einmal gezeichnet, ueber Antworten hinweg aber
 *     immer wieder. Der Entdubelungs-Set lebte urspruenglich fuer die ganze
 *     Sitzung; wer dasselbe Bild erneut anforderte, bekam kommentarlos nichts.
 *  2. Die Pixel landen in einem RESERVIERTEN Block (`kind: 'image'`), der vor
 *     dem Text angemeldet wird — nicht ueber `writeAbove` oberhalb des Frames.
 *     Dort frisst der naechste Repaint sie anteilig zur Framehoehe auf, was am
 *     PC die Fehlplatzierung ganz oben und am Telefon das voellige Fehlen war.
 *  3. Die Terminalmasse reisen mit der RPC. Das Gateway kann sie nicht kennen,
 *     wenn die Sitzung von woanders angehaengt ist.
 */
import type * as InkModule from '@hermes/ink'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createGatewayEventHandler } from '../app/createGatewayEventHandler.js'
import { resetOverlayState } from '../app/overlayStore.js'
import { turnController } from '../app/turnController.js'
import { resetTurnState } from '../app/turnStore.js'
import { resetUiState } from '../app/uiStore.js'
import type { Msg } from '../types.js'

const writeIntoFrameMock = vi.fn((_marker: string, _payload: string) => true)

vi.mock('@hermes/ink', async importActual => ({
  ...(await importActual<typeof InkModule>()),
  writeIntoFrame: (marker: string, payload: string) => writeIntoFrameMock(marker, payload)
}))

const ref = <T,>(current: T) => ({ current })

const SEQUENCE = '\u001b]1337;File=inline=1;size=9:AAAA\u0007'

const buildCtx = (appended: Msg[], rpc: ReturnType<typeof vi.fn>, stdout: Record<string, unknown> = {}) =>
  ({
    composer: {
      dequeue: () => undefined,
      queueEditRef: ref<null | number>(null),
      sendQueued: vi.fn(),
      setInput: vi.fn()
    },
    gateway: { gw: { request: vi.fn() }, rpc },
    session: {
      STARTUP_RESUME_ID: '',
      colsRef: ref(80),
      newSession: vi.fn(),
      resetSession: vi.fn(),
      resumeById: vi.fn(),
      setCatalog: vi.fn()
    },
    submission: { submitRef: { current: vi.fn() } },
    system: { bellOnComplete: false, stdout: { isTTY: true, ...stdout }, sys: vi.fn() },
    transcript: {
      appendMessage: (msg: Msg) => appended.push(msg),
      panel: vi.fn(),
      setHistoryItems: vi.fn()
    },
    voice: { setProcessing: vi.fn(), setRecording: vi.fn(), setVoiceEnabled: vi.fn() }
  }) as any

describe('transcript image delivery', () => {
  beforeEach(() => {
    resetOverlayState()
    resetUiState()
    resetTurnState()
    turnController.fullReset()
    writeIntoFrameMock.mockClear()
    writeIntoFrameMock.mockImplementation(() => true)
    vi.useFakeTimers()
  })

  const completeWith = async (handle: (ev: any) => void, text: string) => {
    handle({ type: 'message.complete', payload: { text } })
    await vi.advanceTimersByTimeAsync(2000)
    await vi.runOnlyPendingTimersAsync()
  }

  const okRpc = () => vi.fn(async () => ({ available: true, cols: 10, rows: 5, sequence: SEQUENCE }))

  it('draws the same image again in a later answer', async () => {
    const appended: Msg[] = []
    const handle = createGatewayEventHandler(buildCtx(appended, okRpc()))

    await completeWith(handle, 'hier: MEDIA:/tmp/a.png')
    const afterFirst = writeIntoFrameMock.mock.calls.length

    await completeWith(handle, 'nochmal: MEDIA:/tmp/a.png')

    expect(afterFirst).toBeGreaterThan(0)
    expect(writeIntoFrameMock.mock.calls.length).toBeGreaterThan(afterFirst)
  })

  it('draws a path named twice in ONE answer only once', async () => {
    const appended: Msg[] = []
    const handle = createGatewayEventHandler(buildCtx(appended, okRpc()))

    await completeWith(handle, 'MEDIA:/tmp/a.png und nochmal MEDIA:/tmp/a.png')

    expect(writeIntoFrameMock).toHaveBeenCalledTimes(1)
  })

  it('reserves a block of the image height AFTER the text message', async () => {
    /* Ohne den reservierten Block gehoeren die Bildzellen nicht dem Frame, und der
     * naechste Repaint holt sie sich zurueck — das war der eigentliche Fehler.
     *
     * Der Text kommt zuerst und SYNCHRON: ihn hinter das `await` der RPC zu schieben
     * war ein Regress, der jeden Leser des Transcripts direkt nach `message.complete`
     * leer ausgehen liess. */
    const appended: Msg[] = []
    const handle = createGatewayEventHandler(buildCtx(appended, okRpc()))

    await completeWith(handle, 'siehe MEDIA:/tmp/a.png')

    const block = appended.findIndex(m => m.kind === 'image')
    const text = appended.findIndex(m => m.kind !== 'image')

    expect(block).toBeGreaterThanOrEqual(0)
    expect(appended[block]!.imageRows).toBe(5)
    expect(appended[block]!.imageMarker).toBeTruthy()
    expect(text).toBeLessThan(block)
  })

  it('appends the answer text synchronously, without waiting for the image RPC', async () => {
    /* Der Regress, den der Block-Umbau verursachte: alles hinter dem `await` zu
     * haengen liess `message.complete` ohne Text zurueck. */
    const appended: Msg[] = []
    const handle = createGatewayEventHandler(buildCtx(appended, okRpc()))

    handle({ type: 'message.complete', payload: { text: 'fertig, MEDIA:/tmp/a.png' } })

    expect(appended.some(m => m.kind !== 'image' && m.text?.includes('fertig'))).toBe(true)
  })

  it('paints each image into its OWN block', async () => {
    /* Zwei Bilder mit demselben Marker wuerden beide im Block des ersten landen:
     * `writeIntoFrame` sucht die erste Zeile, die den Marker traegt. */
    const appended: Msg[] = []
    const handle = createGatewayEventHandler(buildCtx(appended, okRpc()))

    await completeWith(handle, 'MEDIA:/tmp/a.png und MEDIA:/tmp/b.png')

    const marker = writeIntoFrameMock.mock.calls.map(c => c[0])
    expect(marker).toHaveLength(2)
    expect(new Set(marker).size).toBe(2)
  })

  it('sends the terminal size so a narrow pane is not cropped', async () => {
    /* Das Gateway kann die Masse nicht kennen — bei einer angehaengten Sitzung
     * laeuft es auf einer anderen Maschine und kodierte fest 80 Spalten. */
    const rpc = okRpc()
    const handle = createGatewayEventHandler(buildCtx([], rpc, { columns: 34, rows: 20 }))

    await completeWith(handle, 'MEDIA:/tmp/a.png')

    const params = (rpc.mock.calls[0] as unknown as [string, { cols?: number; rows?: number }])[1]
    expect(params.cols).toBeLessThanOrEqual(34)
    expect(params.cols).toBeGreaterThan(0)
    expect(params.rows).toBeLessThanOrEqual(20)
  })

  it('retries across frames while the block is not rendered yet', async () => {
    /* `appendMessage` PLANT den Render nur. Ein einzelner Timer verlor das Bild,
     * wenn der Render spaeter kam — das Loch im Transcript blieb. */
    const handle = createGatewayEventHandler(buildCtx([], okRpc()))
    writeIntoFrameMock.mockImplementation(() => false)
    writeIntoFrameMock.mockImplementationOnce(() => false)

    await completeWith(handle, 'MEDIA:/tmp/a.png')

    expect(writeIntoFrameMock.mock.calls.length).toBeGreaterThan(1)
  })
})

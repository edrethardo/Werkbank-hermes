import type * as InkModule from '@hermes/ink'
/* Wird ein `MEDIA:`-Bild auch beim ZWEITEN Mal gezeigt?
 *
 * Der Entdubelungs-Set lebte urspruenglich fuer die ganze Sitzung. Wer dasselbe
 * Bild ein zweites Mal anforderte, bekam kommentarlos nichts — ununterscheidbar
 * von einem Fehler. Das hier pinnt: pro Antwort einmal, ueber Antworten hinweg
 * immer wieder.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createGatewayEventHandler } from '../app/createGatewayEventHandler.js'
import { resetOverlayState } from '../app/overlayStore.js'
import { turnController } from '../app/turnController.js'
import { resetTurnState } from '../app/turnStore.js'
import { resetUiState } from '../app/uiStore.js'
import type { Msg } from '../types.js'

const writeAboveMock = vi.fn((_payload: string) => true)

vi.mock('@hermes/ink', async importActual => ({
  ...(await importActual<typeof InkModule>()),
  writeAbove: (payload: string) => writeAboveMock(payload)
}))

const ref = <T,>(current: T) => ({ current })

const SEQUENCE = '\u001b]1337;File=inline=1;size=9:AAAA\u0007'

const buildCtx = (appended: Msg[], rpc: ReturnType<typeof vi.fn>) =>
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
    system: { bellOnComplete: false, stdout: { isTTY: true }, sys: vi.fn() },
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
    writeAboveMock.mockClear()
    vi.useFakeTimers()
  })

  const completeWith = async (handle: (ev: any) => void, text: string) => {
    handle({ type: 'message.complete', payload: { text } })
    await vi.advanceTimersByTimeAsync(200)
    await vi.runOnlyPendingTimersAsync()
  }

  it('draws the same image again in a later answer', async () => {
    const rpc = vi.fn(async () => ({ available: true, cols: 10, rows: 5, sequence: SEQUENCE }))
    const appended: Msg[] = []
    const handle = createGatewayEventHandler(buildCtx(appended, rpc))

    await completeWith(handle, 'hier: MEDIA:/tmp/a.png')
    const afterFirst = writeAboveMock.mock.calls.length

    await completeWith(handle, 'nochmal: MEDIA:/tmp/a.png')

    expect(afterFirst).toBeGreaterThan(0)
    expect(writeAboveMock.mock.calls.length).toBeGreaterThan(afterFirst)
  })

  it('draws a path named twice in ONE answer only once', async () => {
    const rpc = vi.fn(async () => ({ available: true, cols: 10, rows: 5, sequence: SEQUENCE }))
    const appended: Msg[] = []
    const handle = createGatewayEventHandler(buildCtx(appended, rpc))

    await completeWith(handle, 'MEDIA:/tmp/a.png und nochmal MEDIA:/tmp/a.png')

    expect(writeAboveMock).toHaveBeenCalledTimes(1)
  })
})

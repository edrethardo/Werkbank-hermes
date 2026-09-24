import { stripAnsi } from '@hermes/shared/ansi'
import { useStore } from '@nanostores/react'
import { atom } from 'nanostores'
import { useMemo } from 'react'

import { $uiState } from './uiStore.js'

// Background `terminal(background=true)` processes owned by this session, as the
// gateway's `process.list` reports them. Session-local presentation only.

export interface ProcessEntry {
  session_id: string
  command?: string
  status?: string
  uptime_seconds?: number | null
  exit_code?: number | null
  exited_at?: number | null
  completion_reason?: string | null
  output_preview?: string
}

export interface ProcessRow {
  command: string
  /** Latest non-empty output line while running; the exit verdict once finished. */
  detail: string
  elapsedSeconds: number
  id: string
  /** The line the process's terminal shows right now (newest progress frame); '' once finished. */
  output: string
  /** Seconds since exit; 0 while running. */
  sinceExitSeconds: number
  status: 'done' | 'failed' | 'killed' | 'lost' | 'running'
}

/** A finished process stays on the dock long enough to read its exit line, then
 * leaves; the completion notification in the transcript is the durable record. */
export const PROCESS_RETAIN_SECONDS = 60

export const $processSnapshot = atom<{ sid: string | null; processes: ProcessEntry[] }>({ sid: null, processes: [] })

// Live output between polls. The gateway already streams every chunk a background
// process writes as `agent.terminal.output` (the desktop's terminal tabs read it);
// the dock keeps a short raw tail per process so a progress bar redraws at stream
// speed instead of the 1.5 s `process.list` cadence. Display only: nothing here is
// sent back, so progress never becomes a message the model has to read.

/** Enough for a few frames of a wide progress bar; only the newest line is shown. */
export const LIVE_TAIL_CHARS = 600
/** Repaint budget for streamed chunks: rsync/tqdm can emit dozens of frames a second. */
export const LIVE_FLUSH_MS = 250
/** A streamed tail older than this yields to the polled preview (missed chunks self-heal). */
export const LIVE_FRESH_MS = 3000

const liveTails = new Map<string, { at: number; tail: string }>()
let liveFlush: ReturnType<typeof setTimeout> | null = null

const withLiveTails = (processes: ProcessEntry[], nowMs: number): ProcessEntry[] =>
  processes.map(entry => {
    const live = liveTails.get(entry.session_id)

    return live && nowMs - live.at <= LIVE_FRESH_MS ? { ...entry, output_preview: live.tail } : entry
  })

const publish = (sid: string | null, processes: ProcessEntry[]) => {
  const previous = $processSnapshot.get()

  if (previous.sid !== sid || JSON.stringify(previous.processes) !== JSON.stringify(processes)) {
    $processSnapshot.set({ sid, processes })
  }
}

export function applyProcessSnapshot(sid: string | null, processes: ProcessEntry[] = []) {
  if ($processSnapshot.get().sid !== sid) {
    liveTails.clear()
  }

  const ids = new Set(processes.map(p => p.session_id))

  for (const id of liveTails.keys()) {
    if (!ids.has(id)) {
      liveTails.delete(id)
    }
  }

  publish(sid, withLiveTails(processes, Date.now()))
}

/** Append one streamed chunk; repaints are coalesced to one per `LIVE_FLUSH_MS`.
 * Chunks for processes the dock does not list (another session's) are dropped. */
export function applyProcessOutput(processId: string, chunk: string, nowMs = Date.now()) {
  const { processes, sid } = $processSnapshot.get()

  if (!processId || !chunk || !processes.some(p => p.session_id === processId)) {
    return
  }

  const tail = ((liveTails.get(processId)?.tail ?? '') + chunk).slice(-LIVE_TAIL_CHARS)
  liveTails.set(processId, { at: nowMs, tail })

  liveFlush ??= setTimeout(() => {
    liveFlush = null
    const current = $processSnapshot.get()

    if (current.sid === sid) {
      publish(sid, withLiveTails(current.processes, Date.now()))
    }
  }, LIVE_FLUSH_MS)
}

const REASON_STATUS: Record<string, ProcessRow['status']> = { failed_start: 'failed', killed: 'killed', lost: 'lost' }

export const processStatus = (entry: ProcessEntry): ProcessRow['status'] => {
  if (entry.status !== 'exited') {
    return 'running'
  }

  return REASON_STATUS[entry.completion_reason ?? ''] ?? (entry.exit_code ? 'failed' : 'done')
}

/** The line a terminal would be showing now. Progress bars (rsync, curl, tqdm, dd)
 * redraw one line with `\r`, so the newest frame is the last `\r`/`\n` segment —
 * splitting on `\n` alone glued every frame of the 200-char tail together and the
 * dock truncated to the OLDEST one. ANSI styling is stripped: the preview is raw
 * process output and escape bytes in an Ink `<Text>` corrupt the row. */
export const lastOutputLine = (preview: string | undefined): string => {
  // Split BEFORE stripping: `stripAnsi` also drops bare `\r`, which would glue the frames back together.
  for (const raw of (preview ?? '').split(/\r\n|\r|\n/).reverse()) {
    const text = stripAnsi(raw).replace(/\s+/g, ' ').trim()

    if (text) {
      return text
    }
  }

  return ''
}

export const processVerdict = (row: ProcessRow, exitCode: number | null | undefined): string => {
  if (row.status === 'running') {
    return row.detail ? `last: ${row.detail}` : 'starting'
  }

  const verdict = row.status === 'killed' || row.status === 'lost' ? row.status : `exit ${exitCode ?? '?'}`

  return `${verdict} · ${row.sinceExitSeconds}s ago`
}

/** Running processes first (longest running first), then recently exited ones
 * newest-exit first; exits older than the retention window are dropped. */
export const buildProcessRows = (processes: readonly ProcessEntry[], nowMs: number): ProcessRow[] => {
  const nowS = nowMs / 1000
  const rows: ProcessRow[] = []

  for (const entry of processes) {
    const status = processStatus(entry)
    const exitedAt = status === 'running' ? 0 : (entry.exited_at ?? 0)
    const sinceExitSeconds = exitedAt ? Math.max(0, Math.floor(nowS - exitedAt)) : 0

    if (status !== 'running' && (!exitedAt || sinceExitSeconds > PROCESS_RETAIN_SECONDS)) {
      continue
    }

    const row: ProcessRow = {
      command: (entry.command ?? '').replace(/\s+/g, ' ').trim() || 'background process',
      detail: lastOutputLine(entry.output_preview),
      elapsedSeconds: Math.max(0, entry.uptime_seconds ?? 0) - sinceExitSeconds,
      id: entry.session_id,
      output: '',
      sinceExitSeconds,
      status
    }

    row.output = status === 'running' ? row.detail : ''
    row.detail = processVerdict(row, entry.exit_code)
    rows.push(row)
  }

  return rows.sort((a, b) =>
    (a.status === 'running') !== (b.status === 'running')
      ? a.status === 'running'
        ? -1
        : 1
      : a.status === 'running'
        ? b.elapsedSeconds - a.elapsedSeconds
        : a.sinceExitSeconds - b.sinceExitSeconds
  )
}

export function useProcessRows(nowMs: number): ProcessRow[] {
  const snapshot = useStore($processSnapshot)
  const { sid } = useStore($uiState)

  return useMemo(() => buildProcessRows(snapshot.sid === sid ? snapshot.processes : [], nowMs), [snapshot, sid, nowMs])
}

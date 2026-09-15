// Werkbank hosts this /chat surface in a frame and may ask the PTY to run one
// of a fixed set of foreign programs instead of Hermes' own TUI (see
// hermes_cli/werkbank_programs.py). The terminal, its input adapter and every
// improvement made to them are shared; only the child process differs.
//
// Kept in a file of its own so an upstream rebase of ChatPage never conflicts.

const PROGRAMS = new Set(["claude-code"]);

export function ptyWerkbankParams(
  search: URLSearchParams, cols?: number, rows?: number,
): Record<string, string> {
  const program = (search.get("program") || "").trim();
  if (!PROGRAMS.has(program)) return {};
  const out: Record<string, string> = { program };
  const project = (search.get("project") || "").trim();
  if (project.startsWith("/")) out.project = project;
  // Spawn at the size the browser already has. A foreign TUI redraws its whole
  // intro on SIGWINCH and appends the redraw, so starting at 80x24 and being
  // resized a moment later leaves two or three stacked copies of the banner.
  if (Number.isFinite(cols) && Number.isFinite(rows) && (cols as number) > 0 && (rows as number) > 0) {
    out.cols = String(Math.trunc(cols as number));
    out.rows = String(Math.trunc(rows as number));
  }
  return out;
}

// Distinct channel per program+project: a Claude Code tab must not share the
// sidebar channel or the keep-alive identity of a Hermes chat.
export function ptyWerkbankChannelKey(search: URLSearchParams): string {
  const p = ptyWerkbankParams(search);
  return `${p.program ?? ""}\0${p.project ?? ""}`;
}

// ── Terminal profile for a hosted foreign program ────────────────────────────
//
// The chat surface is built for Hermes' Ink TUI, which the dashboard starts
// INLINE: its transcript lands in xterm's own 5000-line scrollback, so wheel
// and one-finger pan move that buffer with `term.scrollLines()`.
//
// A foreign program does not play that game. Claude Code switches to the
// alternate screen (measured: \e[?1049h) and turns on mouse tracking
// (\e[?1000h, ?1002h, ?1003h, ?1006h), so xterm's buffer stays EMPTY — the
// gesture computes a travel and then scrolls nothing. Under `tmux attach` the
// history that matters belongs to tmux, and tmux wants the wheel itself.
//
// So for a hosted program the sink changes and nothing else: the SAME gesture
// maths in pty-touch-scroll decides how far the finger moved, and the movement
// goes to the program instead of to a buffer that does not exist. Tuning that
// library keeps tuning both.
//
// This lives here, not in ChatPage: the page hands the terminal over in one
// line and keeps no knowledge of any of it.

import {
  advanceTouchAnchor, isTouchPan, touchLineTravel, touchScrollLines,
} from "@/lib/pty-touch-scroll";

interface ProfileTerminal {
  element?: HTMLElement | null;
  rows: number;
  attachCustomWheelEventHandler(handler: (ev: WheelEvent) => boolean): void;
}

interface ProfileSocket {
  send(data: string): void;
  addEventListener(type: "close", listener: () => void): void;
}

/** SGR mouse wheel, the encoding tmux and every full-screen program expect. */
export function wheelReport(up: boolean, col: number, row: number): string {
  const button = up ? 64 : 65;
  const c = Math.max(1, Math.trunc(col) || 1);
  const r = Math.max(1, Math.trunc(row) || 1);
  return `\x1b[<${button};${c};${r}M`;
}

/**
 * Hand the terminal to the Werkbank profile. A no-op without ?program=, so the
 * page can call it unconditionally.
 *
 * Returns a disposer; it also disposes itself when the socket closes, so the
 * caller does not have to.
 */
export function applyWerkbankTerminalProfile(
  term: ProfileTerminal, socket: ProfileSocket, search: URLSearchParams,
): () => void {
  const noop = () => {};
  if (!ptyWerkbankParams(search).program) return noop;

  // Wheel: hand it back to xterm, which encodes mouse protocol for whatever
  // requested it. The page's own handler swallows every wheel event to scroll
  // its buffer; ours is attached later and replaces it.
  term.attachCustomWheelEventHandler(() => true);

  const host = term.element;
  if (!host) return noop;

  // tmux's copy-mode is a MODE: scrolling up enters it and the prompt does not
  // come back on its own. Count what we sent, so a tap can leave again — and
  // so we never send a stray `q` into a prompt we never scrolled away from.
  let pending = 0;
  const leaveIfScrolled = () => {
    if (pending <= 0) return;
    pending = 0;
    socket.send("q");
  };
  const scroll = (lines: number, col: number, row: number) => {
    const up = lines < 0;
    for (let i = 0; i < Math.abs(lines); i += 1) socket.send(wheelReport(up, col, row));
    pending = Math.max(0, pending + (up ? Math.abs(lines) : -Math.abs(lines)));
    if (pending === 0 && !up) socket.send("q");
  };

  let touchId: number | null = null;
  let anchorY: number | null = null;
  let originY: number | null = null;
  let panning = false;

  const rowHeight = () => {
    const box = host.getBoundingClientRect();
    return term.rows > 0 ? box.height / term.rows : 0;
  };

  const onStart = (ev: TouchEvent) => {
    if (touchId !== null || ev.touches.length !== 1) return;
    const touch = ev.touches[0];
    touchId = touch.identifier;
    anchorY = touch.clientY;
    originY = touch.clientY;
    panning = false;
    ev.stopImmediatePropagation();
  };

  const find = (list: TouchList) => {
    for (let i = 0; i < list.length; i += 1) {
      if (list[i].identifier === touchId) return list[i];
    }
    return null;
  };

  const onMove = (ev: TouchEvent) => {
    if (touchId === null) return;
    const touch = find(ev.touches);
    if (!touch || anchorY === null || originY === null) return;
    ev.stopImmediatePropagation();
    if (!panning && !isTouchPan(originY, touch.clientY)) return;
    panning = true;
    ev.preventDefault();
    const height = rowHeight();
    const lines = touchScrollLines(anchorY, touch.clientY, height);
    if (!lines) return;
    anchorY = advanceTouchAnchor(anchorY, lines, touchLineTravel(height));
    // A finger moving UP the screen means "show me later output" — scroll down.
    scroll(-lines, 1, Math.max(1, Math.trunc(term.rows / 2)));
  };

  const onEnd = (ev: TouchEvent) => {
    if (touchId === null || find(ev.touches)) return;
    const wasPan = panning;
    touchId = null;
    anchorY = null;
    originY = null;
    panning = false;
    ev.stopImmediatePropagation();
    // A tap is „take me back to the prompt", never a keystroke of its own.
    if (!wasPan) leaveIfScrolled();
  };

  host.addEventListener("touchstart", onStart, { capture: true, passive: true });
  host.addEventListener("touchmove", onMove, { capture: true, passive: false });
  host.addEventListener("touchend", onEnd, { capture: true, passive: true });
  host.addEventListener("touchcancel", onEnd, { capture: true, passive: true });

  const dispose = () => {
    host.removeEventListener("touchstart", onStart, true);
    host.removeEventListener("touchmove", onMove, true);
    host.removeEventListener("touchend", onEnd, true);
    host.removeEventListener("touchcancel", onEnd, true);
  };
  socket.addEventListener("close", dispose);
  return dispose;
}

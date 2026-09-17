/**
 * Finger and wheel panning for the dashboard's embedded terminal.
 *
 * xterm's custom wheel hook never sees touch drags, and on a phone iOS Safari
 * additionally synthesizes a wheel event from the *same* finger that produced
 * touchmove. Left alone, the browser tries to scroll the fixed dashboard
 * chrome while the terminal history stays put, and the caret gets yanked by
 * the duplicate wheel. This module is the single owner of "the user is
 * panning the transcript": it keeps one active finger, translates movement
 * into whole terminal rows, swallows the duplicated wheel, and suppresses the
 * click that a pan would otherwise synthesize at lift-off.
 *
 * It also guards the viewport against xterm's reflow jumping to line 0 while a
 * pan is in flight (`shouldRejectViewportJumpToTop`) — call `noteScroll()`
 * from the terminal's `onScroll`.
 *
 * Extracted from ChatPage so the page keeps only the call sites: new files do
 * not conflict when upstream edits ChatPage.
 */

import {
  advanceTouchAnchor,
  isTouchPan,
  touchLineTravel,
  touchScrollLines,
  wheelScrollLines,
} from "@/lib/pty-touch-scroll";
import { shouldRejectViewportJumpToTop } from "@/lib/pty-scroll";

/** The slice of xterm's `Terminal` this module drives. */
export interface PtyPanTerminal {
  readonly rows: number;
  readonly buffer: { active: { viewportY: number } };
  attachCustomWheelEventHandler(handler: (event: WheelEvent) => boolean): void;
  scrollLines(amount: number): void;
  scrollToLine(line: number): void;
}

export interface PtyTouchPanOptions {
  /** True while the pointer is coarse (phone/tablet), i.e. wheel is synthesized. */
  coarsePointer: boolean;
  /** Told when a pan starts/ends so the visual caret can stop tracking. */
  setCaretSuspended?(suspended: boolean): void;
}

export interface PtyTouchPanHandle {
  /** Call from `term.onScroll` — rejects reflow jumps to the top mid-pan. */
  noteScroll(): void;
  dispose(): void;
}

/** Wheel events arriving this soon after a finger pan are the same gesture. */
const SYNTHESIZED_WHEEL_WINDOW_MS = 450;
/** A single terminal row never scrolls further than this many pixels. */
const MAX_ROW_HEIGHT_PX = 36;

export function installPtyTouchPan(
  term: PtyPanTerminal,
  host: HTMLElement,
  touchRoot: HTMLElement,
  { coarsePointer, setCaretSuspended }: PtyTouchPanOptions,
): PtyTouchPanHandle {
  let lastTouchScrollAt = 0;
  let touchId: number | null = null;
  let touchY: number | null = null;
  let touchOriginY: number | null = null;
  let touchPanning = false;
  let suppressClickAfterPan = false;
  let lastViewportY = 0;
  let restoringViewport = false;

  // Dashboard chat should scroll the browser-side transcript, not send
  // mouse-wheel protocol bytes through the PTY. On a coarse pointer the wheel
  // is a duplicate of the finger we already handle — drop it outright.
  term.attachCustomWheelEventHandler((ev) => {
    if (coarsePointer || Date.now() - lastTouchScrollAt < SYNTHESIZED_WHEEL_WINDOW_MS) {
      ev.preventDefault();
      ev.stopPropagation();
      return false;
    }
    const lines = wheelScrollLines(ev.deltaY);
    if (!lines) {
      return false;
    }

    term.scrollLines(lines);

    ev.preventDefault();
    ev.stopPropagation();
    return false;
  });

  touchRoot.style.touchAction = "none";

  const activeTouch = (list: TouchList) => {
    for (let i = 0; i < list.length; i += 1) {
      if (list[i].identifier === touchId) return list[i];
    }
    return null;
  };
  const onTouchStart = (ev: TouchEvent) => {
    if (ev.touches.length !== 1) {
      touchId = null;
      touchY = null;
      touchOriginY = null;
      touchPanning = false;
      return;
    }
    touchId = ev.touches[0].identifier;
    touchY = ev.touches[0].clientY;
    touchOriginY = ev.touches[0].clientY;
    touchPanning = false;
  };
  const onTouchMove = (ev: TouchEvent) => {
    if (ev.touches.length !== 1 || touchId === null || touchY === null || touchOriginY === null) return;
    const touch = activeTouch(ev.touches);
    if (!touch) return;
    ev.preventDefault();
    ev.stopPropagation();
    if (!touchPanning && !isTouchPan(touchOriginY, touch.clientY)) {
      return;
    }
    touchPanning = true;
    setCaretSuspended?.(true);
    const rowHeight = Math.min(
      MAX_ROW_HEIGHT_PX,
      Math.max(1, host.clientHeight / Math.max(1, term.rows)),
    );
    const lines = touchScrollLines(touchY, touch.clientY, rowHeight);
    if (lines) {
      touchY = advanceTouchAnchor(touchY, lines, touchLineTravel(rowHeight));
      term.scrollLines(lines);
      lastTouchScrollAt = Date.now();
    }
  };
  const onTouchEnd = (ev: TouchEvent) => {
    if (!activeTouch(ev.touches)) {
      if (touchPanning) suppressClickAfterPan = true;
      setCaretSuspended?.(false);
      touchId = null;
      touchY = null;
      touchOriginY = null;
      touchPanning = false;
    }
  };
  const onSuppressedClick = (ev: Event) => {
    if (!suppressClickAfterPan) return;
    suppressClickAfterPan = false;
    ev.preventDefault();
    ev.stopPropagation();
  };

  touchRoot.addEventListener("touchstart", onTouchStart, { passive: true, capture: true });
  touchRoot.addEventListener("touchmove", onTouchMove, { passive: false, capture: true });
  touchRoot.addEventListener("touchend", onTouchEnd, { passive: true, capture: true });
  touchRoot.addEventListener("touchcancel", onTouchEnd, { passive: true, capture: true });
  host.addEventListener("mousedown", onSuppressedClick, true);
  host.addEventListener("click", onSuppressedClick, true);

  return {
    noteScroll() {
      const nextY = term.buffer.active.viewportY;
      if (!restoringViewport && shouldRejectViewportJumpToTop(lastViewportY, nextY, touchPanning)) {
        restoringViewport = true;
        try {
          term.scrollToLine(lastViewportY);
        } catch {
          /* ignore */
        } finally {
          restoringViewport = false;
        }
      } else {
        lastViewportY = nextY;
      }
    },
    dispose() {
      touchRoot.removeEventListener("touchstart", onTouchStart, true);
      touchRoot.removeEventListener("touchmove", onTouchMove, true);
      touchRoot.removeEventListener("touchend", onTouchEnd, true);
      touchRoot.removeEventListener("touchcancel", onTouchEnd, true);
      host.removeEventListener("mousedown", onSuppressedClick, true);
      host.removeEventListener("click", onSuppressedClick, true);
    },
  };
}

/**
 * Stop xterm's own viewport from scrolling on a coarse pointer — this module
 * moves the buffer instead, and two scrollers fight each other on a phone.
 */
export function lockPtyViewportScrolling(host: HTMLElement): void {
  const viewport = host.querySelector<HTMLElement>(".xterm-viewport");
  if (!viewport) return;
  viewport.style.overflowY = "hidden";
  viewport.style.overscrollBehavior = "none";
}

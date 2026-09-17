/**
 * Soft-keyboard choreography for the dashboard's embedded terminal.
 *
 * `keyboard-inset.ts` owns the arithmetic (how many pixels are obscured, how
 * far to scroll). This module owns the *sequence* built on top of it: reserve
 * room below the terminal, show or hide the mobile accessory bar, pull the
 * composer above the keyboard across the frames in which iOS is still
 * animating, and re-run the whole thing when the terminal's textarea gains or
 * loses focus.
 *
 * Extracted from ChatPage so the page keeps only `install` / `sync` / `reset`
 * / `dispose`: new files do not conflict when upstream edits ChatPage.
 */

import {
  computeKeyboardInset,
  keyboardRevealScrollDelta,
  shouldJumpViewportForKeyboard,
  shouldScrollChatIntoView,
} from "@/lib/keyboard-inset";
import {
  ACCESSORY_BAR_HEIGHT_PX,
  shouldShowMobileAccessory,
  terminalBottomReservePx,
} from "@/lib/pty-mobile-accessory";

/** The slice of xterm's `Terminal` this module drives. */
export interface PtyKeyboardTerminal {
  readonly textarea: HTMLTextAreaElement | null | undefined;
  scrollToBottom(): void;
}

/** The slice of the mounted accessory bar this module drives. */
export interface PtyKeyboardAccessory {
  setInset(inset: number, phoneChrome: boolean, focused: boolean): void;
}

export interface PtyKeyboardRevealOptions {
  term: PtyKeyboardTerminal;
  /** The xterm host element — its bottom edge is the composer's bottom edge. */
  host: HTMLElement;
  /** The wrapper whose bottom padding reserves room for the keyboard. */
  wrap: HTMLElement | null;
  accessory: PtyKeyboardAccessory;
  /** Called when the reserved height changed and the grid must refit. */
  onHostResize(): void;
  /** Called after every viewport change so cell metrics can be re-measured. */
  onViewportSettled(): void;
}

export interface PtyKeyboardRevealHandle {
  /** Re-read the viewport and apply the inset. Safe to call at any time. */
  sync(): void;
  /** Forget the applied inset and drop the reserved padding. */
  reset(): void;
  dispose(): void;
}

/** iOS keeps animating after focus; re-measure once the animation settled. */
const KEYBOARD_SETTLE_MS = 350;

export function installPtyKeyboardReveal({
  term,
  host,
  wrap,
  accessory,
  onHostResize,
  onViewportSettled,
}: PtyKeyboardRevealOptions): PtyKeyboardRevealHandle {
  let appliedKeyboardInset = 0;
  let settleTimer = 0;

  const syncKeyboardInset = () => {
    if (!wrap) return;
    const vv = window.visualViewport;
    const inset = computeKeyboardInset(
      vv ? { height: vv.height, offsetTop: vv.offsetTop } : null,
      window.innerHeight,
    );
    const chatTop = wrap.getBoundingClientRect().top;
    const inIframe = window.self !== window.top;
    const phoneChrome =
      (typeof window.matchMedia === "function" &&
        window.matchMedia("(pointer: coarse)").matches) ||
      navigator.maxTouchPoints > 0;
    const focused = term.textarea === document.activeElement;
    const showBar = shouldShowMobileAccessory(inset, phoneChrome, focused);
    const barPx = showBar ? ACCESSORY_BAR_HEIGHT_PX : 0;
    const reserve = terminalBottomReservePx(inset, showBar);
    const jumpViewport = shouldJumpViewportForKeyboard(appliedKeyboardInset, reserve);
    if (jumpViewport) {
      appliedKeyboardInset = reserve;
      wrap.style.paddingBottom = reserve > 0 ? `${reserve}px` : "";
      onHostResize();
    }
    accessory.setInset(inset, phoneChrome, focused);
    if (!jumpViewport) return;
    if (shouldScrollChatIntoView(inset, chatTop, inIframe)) {
      wrap.scrollIntoView({ block: "end", inline: "nearest" });
    }
    const revealComposer = () => {
      if (inset <= 0 || !vv) return;
      try {
        term.scrollToBottom();
      } catch {
        /* ignore */
      }
      const delta = keyboardRevealScrollDelta(
        host.getBoundingClientRect().bottom,
        { height: vv.height, offsetTop: vv.offsetTop },
        barPx,
      );
      if (delta) window.scrollBy(0, delta);
    };
    if (inset > 0) {
      revealComposer();
      requestAnimationFrame(() => {
        revealComposer();
        requestAnimationFrame(revealComposer);
      });
    }
  };

  const sync = () => {
    syncKeyboardInset();
    onViewportSettled();
  };

  const onTerminalFocus = () => {
    sync();
    window.clearTimeout(settleTimer);
    settleTimer = window.setTimeout(sync, KEYBOARD_SETTLE_MS);
  };
  const onTerminalBlur = () => {
    window.setTimeout(sync, 0);
  };
  term.textarea?.addEventListener("focus", onTerminalFocus);
  term.textarea?.addEventListener("blur", onTerminalBlur);

  return {
    sync,
    reset() {
      appliedKeyboardInset = 0;
      if (wrap) wrap.style.paddingBottom = "";
    },
    dispose() {
      window.clearTimeout(settleTimer);
      term.textarea?.removeEventListener("focus", onTerminalFocus);
      term.textarea?.removeEventListener("blur", onTerminalBlur);
    },
  };
}

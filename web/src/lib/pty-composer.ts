/**
 * Wires the browser-side input owner, the dictation-ready textarea, and the
 * mobile accessory bar onto one terminal.
 *
 * Each of those three has its own module already; what lived in ChatPage was
 * only the knot between them — the accessory's buttons need the input owner's
 * paste/caret methods, and the textarea layout watcher needs the terminal's
 * measured screen. Tying them together here keeps ChatPage down to a single
 * call and its cleanup down to a single `dispose()`.
 */

import type { Terminal } from "@xterm/xterm";

import {
  composerTextareaBox,
  preparePtyTextareaForDictation,
  watchPtyTextareaLayout,
} from "@/lib/pty-ios-textarea";
import { installPtyBrowserInput } from "@/lib/pty-browser-input";
import { PTY_ETX, mountPtyMobileAccessory } from "@/lib/pty-mobile-accessory";

type BrowserInput = ReturnType<typeof installPtyBrowserInput>;
type Accessory = ReturnType<typeof mountPtyMobileAccessory>;

export interface PtyComposerHandle {
  browserInput: BrowserInput;
  accessory: Accessory;
  dispose(): void;
}

export interface PtyComposerOptions {
  /** True while the socket is open and the PTY accepts input. */
  canSend(): boolean;
  /** Sends a raw control sequence (shortcut path, bypasses the mirror). */
  sendSequence(data: string): boolean;
}

export function installPtyComposer(
  term: Terminal,
  host: HTMLElement,
  accessoryRoot: HTMLElement,
  { canSend, sendSequence }: PtyComposerOptions,
): PtyComposerHandle {
  const browserInput = installPtyBrowserInput(term, canSend, sendSequence);

  const textarea = term.textarea;
  if (textarea) preparePtyTextareaForDictation(textarea);
  const stopWatchingTextarea = textarea
    ? watchPtyTextareaLayout(textarea, () => {
        const screen = term.element?.querySelector<HTMLElement>(".xterm-screen");
        return composerTextareaBox(term.rows, screen?.clientHeight ?? host.clientHeight);
      })
    : () => {};

  const accessory = mountPtyMobileAccessory(accessoryRoot, {
    paste: () => {
      void navigator.clipboard
        .readText()
        .then((text) => {
          if (text) browserInput.paste(text);
        })
        .catch(() => {
          /* Safari may deny clipboard without a gesture */
        });
    },
    interrupt: () => {
      term.input(PTY_ETX);
    },
    caretLeft: () => {
      browserInput.nudge("ArrowLeft");
    },
    caretRight: () => {
      browserInput.nudge("ArrowRight");
    },
    historyUp: () => {
      term.input("\x1b[A", true);
    },
    historyDown: () => {
      term.input("\x1b[B", true);
    },
  });

  return {
    browserInput,
    accessory,
    dispose() {
      browserInput.dispose();
      stopWatchingTextarea();
      accessory.dispose();
    },
  };
}

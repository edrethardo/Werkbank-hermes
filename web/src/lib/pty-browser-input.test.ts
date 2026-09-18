// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";

import { installPtyBrowserInput } from "./pty-browser-input";

function fakeTerm(sendBytes?: (data: string) => boolean, syncInkCaret = true) {
  const host = document.createElement("div");
  const screen = document.createElement("div");
  screen.className = "xterm-screen";
  const textarea = document.createElement("textarea");
  host.append(screen, textarea);
  document.body.append(host);
  const inputs: string[] = [];
  const pastes: string[] = [];
  const dataListeners: Array<(data: string) => void> = [];
  const renderListeners: Array<() => void> = [];
  const term = {
    textarea,
    element: host,
    cols: 80,
    rows: 24,
    options: { fontFamily: "monospace", fontSize: 14, theme: { cursor: "#fff", foreground: "#fff", background: "#000" } },
    buffer: {
      active: {
        baseY: 0,
        cursorY: 0,
        cursorX: 0,
        viewportY: 0,
        getLine: () => undefined,
      },
    },
    input(data: string) { inputs.push(data); },
    paste(data: string) { pastes.push(data); },
    onData(cb?: (data: string) => void) {
      if (cb) dataListeners.push(cb);
      return { dispose() {} };
    },
    emitData(data: string) { dataListeners.forEach((cb) => cb(data)); },
    onSelectionChange() { return { dispose() {} }; },
    onRender(cb?: () => void) {
      if (cb) renderListeners.push(cb);
      return { dispose() {} };
    },
    emitRender() { renderListeners.forEach((cb) => cb()); },
    onResize() { return { dispose() {} }; },
    onScroll() { return { dispose() {} }; },
    hasSelection() { return false; },
  };
  const adapter = installPtyBrowserInput(term as never, () => true, sendBytes, syncInkCaret);
  return { term, textarea, host, inputs, pastes, adapter };
}

/** Apply the byte stream the adapter sent the way an Ink/readline input line
 * would, so a test can assert what the remote line actually contains. */
function replayPtyLine(stream: string): string {
  let line = "";
  let caret = 0;
  for (let i = 0; i < stream.length; i++) {
    const ch = stream[i];
    if (ch === "\x1b" && stream.slice(i, i + 3) === "\x1b[C") { caret = Math.min(line.length, caret + 1); i += 2; continue; }
    if (ch === "\x1b" && stream.slice(i, i + 3) === "\x1b[D") { caret = Math.max(0, caret - 1); i += 2; continue; }
    if (ch === "\x7f") { if (caret > 0) { line = line.slice(0, caret - 1) + line.slice(caret); caret--; } continue; }
    line = line.slice(0, caret) + ch + line.slice(caret);
    caret++;
  }
  return line;
}

describe("pty browser input", () => {
  it("sends keyless dictation insertText to the PTY without a keydown", () => {
    const { textarea, host, inputs, adapter } = fakeTerm();
    textarea.value = "Hallo Welt";
    textarea.setSelectionRange(10, 10);
    textarea.dispatchEvent(new InputEvent("input", {
      bubbles: true,
      inputType: "insertText",
      data: "Hallo Welt",
    }));
    expect(inputs.join("")).toBe("Hallo Welt");
    adapter.dispose();
    host.remove();
  });

  // iOS dictation inserts keyless, then corrects the whole phrase. The adapter
  // must keep believing what the PTY actually holds, or the correction erases
  // too little and writes the opening fragment a second time (WB-520).
  it("does not repeat an earlier phrase when dictation corrects itself", () => {
    const { textarea, host, inputs, adapter } = fakeTerm();
    const dictate = (value: string, data: string) => {
      textarea.value = value;
      textarea.setSelectionRange(value.length, value.length);
      textarea.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data }));
    };
    dictate("Satz eins.", "Satz eins.");
    dictate("Satz eins. Satz zwei.", " Satz zwei.");

    // Safari now replaces the whole dictated phrase, this time with a
    // beforeinput range (the ordinary correction path).
    const before = textarea.value;
    const grown = "Satz eins. Satz zwei. Satz drei.";
    textarea.dispatchEvent(new InputEvent("beforeinput", {
      bubbles: true, cancelable: true, inputType: "insertReplacementText", data: grown,
    }));
    textarea.value = grown;
    textarea.setSelectionRange(grown.length, grown.length);
    textarea.dispatchEvent(new InputEvent("input", {
      bubbles: true, inputType: "insertReplacementText", data: grown,
    }));
    expect(before).toBe("Satz eins. Satz zwei.");
    expect(replayPtyLine(inputs.join(""))).toBe(grown);
    adapter.dispose();
    host.remove();
  });

  // The same self-correction, but the way iOS dictation actually delivers it:
  // a replacement with NO `beforeinput` range. The `!edit` branch refuses the
  // payload (correctly — the DOM does not reveal which range was replaced), but
  // then cleared its model of the PTY and BLANKED the textarea. The terminal
  // keeps showing the old line while the mirror says empty: the user sees the
  // dictated text turn into an unreadable block, and the next correction diffs
  // against nothing and writes the phrase again (WB-520 for the keyless path).
  it("keeps the line intact when keyless dictation replaces without beforeinput", () => {
    const { textarea, host, inputs, adapter } = fakeTerm();
    const dictate = (value: string, data: string) => {
      textarea.value = value;
      textarea.setSelectionRange(value.length, value.length);
      textarea.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data }));
    };
    dictate("Satz eins.", "Satz eins.");
    dictate("Satz eins. Satz zwei.", " Satz zwei.");
    expect(replayPtyLine(inputs.join(""))).toBe("Satz eins. Satz zwei.");

    const corrected = "Satz eins. Satz drei.";
    textarea.value = corrected;
    textarea.setSelectionRange(corrected.length, corrected.length);
    textarea.dispatchEvent(new InputEvent("input", {
      bubbles: true, inputType: "insertReplacementText", data: corrected,
    }));

    // The mirror must still describe the remote line: neither blanked nor
    // desynced. Whatever the adapter chose to send, replaying it has to leave
    // the PTY holding exactly one copy of the text.
    const line = replayPtyLine(inputs.join(""));
    expect(line).not.toContain("Satz eins. Satz eins.");
    expect(textarea.value).toBe(line);
    adapter.dispose();
    host.remove();
  });

  it("paints the preedit legibly at the cursor cell, not transparently at the helper origin", () => {
    const { term, textarea, host, adapter } = fakeTerm();
    const view = document.createElement("div");
    view.className = "composition-view";
    host.append(view);
    // jsdom has no layout: give the screen a real box so the cell math is exercised.
    const screen = host.querySelector(".xterm-screen")!;
    screen.getBoundingClientRect = () => ({ x: 0, y: 0, width: 800, height: 480, top: 0, left: 0, right: 800, bottom: 480, toJSON: () => ({}) });
    term.buffer.active.cursorX = 10; // 10 * (800/80) = 100px
    term.buffer.active.cursorY = 5; //  5 * (480/24) = 100px
    // Rebuild the adapter now that the preedit element exists.
    adapter.dispose();
    const live = installPtyBrowserInput(term as never, () => true);
    term.options.theme = { cursor: "#fff", foreground: "#e6e6e6", background: "#000000" };
    textarea.style.left = "0px";
    textarea.style.top = "999px"; // where the docked phone composer sits
    textarea.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true, data: "" }));
    textarea.dispatchEvent(new CompositionEvent("compositionupdate", { bubbles: true, data: "diktat" }));
    expect(view.textContent).toBe("diktat");
    expect(view.classList.contains("active")).toBe(true);
    // Readable and opaque: composition sends no bytes, so this IS the dictated text.
    expect(view.style.getPropertyValue("color")).not.toBe("transparent");
    expect(view.style.getPropertyValue("-webkit-text-fill-color")).not.toBe("transparent");
    expect(view.style.getPropertyValue("background")).not.toBe("transparent");
    // On the cursor cell, never on the docked composer's coordinates.
    expect(view.style.left).toBe("100px");
    expect(view.style.top).toBe("100px");
    // A redraw moves the composer row (the TUI repaints constantly while dictating). The
    // preedit must follow it, or the dictated words render on some unrelated older line.
    term.buffer.active.cursorY = 20;
    term.emitRender();
    expect(view.style.top).toBe("400px"); // 20 * (480/24)
    live.dispose();
    host.remove();
  });

  it("pastes clipboard text once through the public paste API", () => {
    const { textarea, host, pastes, inputs, adapter } = fakeTerm();
    const event = new Event("paste", { bubbles: true, cancelable: true }) as ClipboardEvent;
    Object.defineProperty(event, "clipboardData", {
      value: { getData: (type: string) => type === "text/plain" ? "eingefuegt" : "" },
    });
    textarea.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    expect(pastes).toEqual(["eingefuegt"]);
    expect(inputs).toEqual([]);
    adapter.dispose();
    host.remove();
  });

  it("does not paint a second caret under the terminal cursor", () => {
    const { host, adapter } = fakeTerm();
    const caret = document.querySelector(".pty-native-caret") as HTMLElement;
    expect(caret).toBeInstanceOf(HTMLElement);
    expect(caret.style.display).toBe("none");
    adapter.dispose();
    host.remove();
    expect(document.querySelector(".pty-native-caret")).toBeNull();
  });

  it("does not wipe the composer on blur so the caret can be recaptured", () => {
    const { textarea, host, adapter } = fakeTerm();
    textarea.value = "Hallo";
    textarea.setSelectionRange(5, 5);
    textarea.dispatchEvent(new InputEvent("input", {
      bubbles: true,
      inputType: "insertText",
      data: "Hallo",
    }));
    textarea.dispatchEvent(new Event("blur", { bubbles: true }));
    expect(textarea.value).toBe("Hallo");
    adapter.nudge("ArrowLeft");
    expect(textarea.selectionStart).toBe(4);
    adapter.dispose();
    host.remove();
  });

  it("does not drop the composer when xterm reports unrelated onData", () => {
    const { textarea, host, adapter, term } = fakeTerm();
    textarea.value = "Hi";
    textarea.setSelectionRange(2, 2);
    textarea.dispatchEvent(new InputEvent("input", {
      bubbles: true,
      inputType: "insertText",
      data: "Hi",
    }));
    term.emitData("x");
    expect(textarea.value).toBe("Hi");
    adapter.nudge("ArrowLeft");
    expect(textarea.selectionStart).toBe(1);
    adapter.dispose();
    host.remove();
  });

  it("sends Ink arrow sequences so the visible TUI cursor moves", () => {
    const { textarea, host, adapter, inputs } = fakeTerm();
    textarea.value = "Hi";
    textarea.setSelectionRange(2, 2);
    textarea.dispatchEvent(new InputEvent("input", {
      bubbles: true,
      inputType: "insertText",
      data: "Hi",
    }));
    adapter.nudge("ArrowLeft");
    expect(inputs.join("")).toContain("\x1b[D");
    adapter.nudge("ArrowRight");
    expect(inputs.join("")).toContain("\x1b[C");
    adapter.dispose();
    host.remove();
  });

  it("sends visible-cursor CSI through the PTY socket path, not only term.input", () => {
    const sent: string[] = [];
    const { textarea, host, adapter, inputs } = fakeTerm((data) => {
      sent.push(data);
      return true;
    });
    textarea.value = "Hi";
    textarea.setSelectionRange(2, 2);
    textarea.dispatchEvent(new InputEvent("input", {
      bubbles: true,
      inputType: "insertText",
      data: "Hi",
    }));
    adapter.nudge("ArrowLeft");
    expect(sent.join("")).toContain("\x1b[D");
    expect(inputs.join("")).not.toContain("\x1b[D");
    adapter.dispose();
    host.remove();
  });

  it("moves the native caret without CSI when Ink cursor sync is off", () => {
    const { textarea, host, adapter, inputs } = fakeTerm(undefined, false);
    textarea.value = "Hi";
    textarea.setSelectionRange(2, 2);
    textarea.dispatchEvent(new InputEvent("input", {
      bubbles: true,
      inputType: "insertText",
      data: "Hi",
    }));
    adapter.nudge("ArrowLeft");
    expect(textarea.selectionStart).toBe(1);
    expect(inputs.join("")).toBe("Hi");
    adapter.dispose();
    host.remove();
  });

  it("sends clamped CSI when the native caret is dragged so the visible cursor follows", () => {
    const { textarea, host, adapter, inputs } = fakeTerm();
    textarea.value = "Hi";
    textarea.setSelectionRange(2, 2);
    textarea.dispatchEvent(new InputEvent("input", {
      bubbles: true,
      inputType: "insertText",
      data: "Hi",
    }));
    textarea.focus();
    textarea.setSelectionRange(0, 0);
    document.dispatchEvent(new Event("selectionchange"));
    expect(inputs.join("")).toBe("Hi\x1b[D\x1b[D");
    adapter.dispose();
    host.remove();
  });

  it("does not refocus the helper on Left/Right so iOS keeps the keyboard open", () => {
    const { textarea, host, adapter } = fakeTerm();
    textarea.value = "Hi";
    textarea.setSelectionRange(2, 2);
    textarea.dispatchEvent(new InputEvent("input", {
      bubbles: true,
      inputType: "insertText",
      data: "Hi",
    }));
    textarea.focus();
    const focus = vi.spyOn(textarea, "focus");
    adapter.nudge("ArrowLeft");
    adapter.nudge("ArrowLeft");
    adapter.nudge("ArrowRight");
    expect(focus).not.toHaveBeenCalled();
    expect(textarea.selectionStart).toBe(1);
    adapter.dispose();
    host.remove();
  });

  it("lets ArrowUp/ArrowDown through so the TUI can cycle command history", () => {
    const { textarea, host, adapter, inputs } = fakeTerm();
    textarea.value = "Hi";
    textarea.setSelectionRange(2, 2);
    textarea.dispatchEvent(new InputEvent("input", {
      bubbles: true,
      inputType: "insertText",
      data: "Hi",
    }));
    const up = new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true, cancelable: true });
    textarea.dispatchEvent(up);
    expect(up.defaultPrevented).toBe(false);
    expect(textarea.value).toBe("Hi");
    expect(inputs.join("")).toBe("Hi");
    adapter.dispose();
    host.remove();
  });

  // Reported as "wenn ich zu viele Zeichen mit Backspace lösche, lädt die
  // Seite kaputt neu". A long delete run must stay one DEL per grapheme with
  // no caret bytes and no reset — anything else desynchronises the mirror and
  // makes the user reach for a reload.
  it("erases exactly one grapheme per Backspace over a long delete run", () => {
    const { textarea, host, inputs, adapter } = fakeTerm();
    const seed = "abcdefghij".repeat(12); // 120 chars
    textarea.value = seed;
    textarea.setSelectionRange(seed.length, seed.length);
    textarea.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: seed }));
    const seedBytes = inputs.join("").length;

    let value = seed;
    for (let i = 0; i < seed.length; i++) {
      const init = { key: "Backspace", code: "Backspace", bubbles: true, cancelable: true };
      textarea.dispatchEvent(new KeyboardEvent("keydown", init));
      textarea.dispatchEvent(new InputEvent("beforeinput", {
        bubbles: true,
        cancelable: true,
        inputType: "deleteContentBackward",
        data: null,
      }));
      value = value.slice(0, -1);
      textarea.value = value;
      textarea.setSelectionRange(value.length, value.length);
      document.dispatchEvent(new Event("selectionchange"));
      textarea.dispatchEvent(new InputEvent("input", {
        bubbles: true,
        inputType: "deleteContentBackward",
        data: null,
      }));
      textarea.dispatchEvent(new KeyboardEvent("keyup", init));
    }

    const deleted = inputs.join("").slice(seedBytes);
    expect(deleted).toBe("\x7f".repeat(seed.length));
    expect(replayPtyLine(inputs.join(""))).toBe("");
    expect(textarea.value).toBe("");
    adapter.dispose();
    host.remove();
  });

  // Auto-repeat: iOS delivers many keydowns before a single keyup. The FIFO
  // must not turn a held Backspace into a burst of duplicate deletes.
  it("does not over-delete when Backspace auto-repeats before its keyup", () => {
    const { textarea, host, inputs, adapter } = fakeTerm();
    const seed = "Hallo Welt";
    textarea.value = seed;
    textarea.setSelectionRange(seed.length, seed.length);
    textarea.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: seed }));
    const seedBytes = inputs.join("").length;

    const init = { key: "Backspace", code: "Backspace", bubbles: true, cancelable: true };
    let value = seed;
    for (let i = 0; i < 4; i++) {
      textarea.dispatchEvent(new KeyboardEvent("keydown", init));
      textarea.dispatchEvent(new InputEvent("beforeinput", {
        bubbles: true, cancelable: true, inputType: "deleteContentBackward", data: null,
      }));
      value = value.slice(0, -1);
      textarea.value = value;
      textarea.setSelectionRange(value.length, value.length);
      textarea.dispatchEvent(new InputEvent("input", {
        bubbles: true, inputType: "deleteContentBackward", data: null,
      }));
    }
    textarea.dispatchEvent(new KeyboardEvent("keyup", init));

    expect(inputs.join("").slice(seedBytes)).toBe("\x7f".repeat(4));
    expect(replayPtyLine(inputs.join(""))).toBe("Hallo ");
    adapter.dispose();
    host.remove();
  });

  // Deleting past the start must not send DEL the PTY would apply to text the
  // adapter no longer owns (that is how a composer gets torn up and the user
  // reloads).
  it("stops sending deletes once the mirror is empty", () => {
    const { textarea, host, inputs, adapter } = fakeTerm();
    textarea.value = "ab";
    textarea.setSelectionRange(2, 2);
    textarea.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: "ab" }));
    const seedBytes = inputs.join("").length;

    let value = "ab";
    for (let i = 0; i < 6; i++) {
      const init = { key: "Backspace", code: "Backspace", bubbles: true, cancelable: true };
      textarea.dispatchEvent(new KeyboardEvent("keydown", init));
      if (value) {
        textarea.dispatchEvent(new InputEvent("beforeinput", {
          bubbles: true, cancelable: true, inputType: "deleteContentBackward", data: null,
        }));
        value = value.slice(0, -1);
        textarea.value = value;
        textarea.setSelectionRange(value.length, value.length);
        textarea.dispatchEvent(new InputEvent("input", {
          bubbles: true, inputType: "deleteContentBackward", data: null,
        }));
      }
      textarea.dispatchEvent(new KeyboardEvent("keyup", init));
    }

    expect(inputs.join("").slice(seedBytes)).toBe("\x7f\x7f");
    adapter.dispose();
    host.remove();
  });
});

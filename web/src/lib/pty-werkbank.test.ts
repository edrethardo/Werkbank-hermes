// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";

import {
  applyWerkbankTerminalProfile, ptyWerkbankChannelKey, ptyWerkbankParams, wheelReport,
} from "./pty-werkbank";

const claudeCode = new URLSearchParams("program=claude-code&project=/data/projects/x");
const hermes = new URLSearchParams("resume=abc");

function fakeTerm(element: HTMLElement | null = document.createElement("div")) {
  return {
    element,
    rows: 40,
    wheelHandler: null as null | ((ev: WheelEvent) => boolean),
    attachCustomWheelEventHandler(handler: (ev: WheelEvent) => boolean) {
      this.wheelHandler = handler;
    },
  };
}

function fakeSocket() {
  return {
    sent: [] as string[],
    closers: [] as Array<() => void>,
    send(data: string) { this.sent.push(data); },
    addEventListener(_type: "close", listener: () => void) { this.closers.push(listener); },
  };
}

describe("params", () => {
  it("passes only an allowlisted program", () => {
    expect(ptyWerkbankParams(new URLSearchParams("program=bash")).program).toBeUndefined();
    expect(ptyWerkbankParams(claudeCode).program).toBe("claude-code");
  });

  it("refuses a project that is not an absolute path", () => {
    const params = ptyWerkbankParams(new URLSearchParams("program=claude-code&project=../etc"));
    expect(params.project).toBeUndefined();
  });

  it("carries the terminal size so the program spawns at it", () => {
    expect(ptyWerkbankParams(claudeCode, 120, 40)).toMatchObject({ cols: "120", rows: "40" });
    expect(ptyWerkbankParams(claudeCode, 0, -1).cols).toBeUndefined();
  });

  it("keys the channel on program and project", () => {
    expect(ptyWerkbankChannelKey(claudeCode)).not.toBe(ptyWerkbankChannelKey(hermes));
  });
});

describe("wheelReport", () => {
  it("is SGR mouse, up 64 and down 65", () => {
    expect(wheelReport(true, 1, 20)).toBe("\x1b[<64;1;20M");
    expect(wheelReport(false, 1, 20)).toBe("\x1b[<65;1;20M");
  });

  it("never emits a zero or fractional coordinate", () => {
    expect(wheelReport(true, 0, -3)).toBe("\x1b[<64;1;1M");
    expect(wheelReport(true, 2.7, 9.2)).toBe("\x1b[<64;2;9M");
  });
});

describe("applyWerkbankTerminalProfile", () => {
  it("does nothing at all for a Hermes chat", () => {
    const term = fakeTerm();
    applyWerkbankTerminalProfile(term, fakeSocket(), hermes);
    expect(term.wheelHandler).toBeNull();
  });

  it("gives the wheel back to xterm so the program receives it", () => {
    const term = fakeTerm();
    applyWerkbankTerminalProfile(term, fakeSocket(), claudeCode);
    expect(term.wheelHandler).not.toBeNull();
    // true = xterm does its default, which is to encode mouse protocol for
    // whatever asked for it. The page's own handler returns false and swallows.
    expect(term.wheelHandler?.(new WheelEvent("wheel", { deltaY: 100 }))).toBe(true);
  });

  it("survives a terminal that has not been attached to the DOM", () => {
    expect(() => applyWerkbankTerminalProfile(fakeTerm(null), fakeSocket(), claudeCode))
      .not.toThrow();
  });

  it("removes its listeners when the socket closes", () => {
    const element = document.createElement("div");
    const remove = vi.spyOn(element, "removeEventListener");
    const socket = fakeSocket();
    applyWerkbankTerminalProfile(fakeTerm(element), socket, claudeCode);
    expect(socket.closers).toHaveLength(1);
    socket.closers[0]();
    expect(remove).toHaveBeenCalledTimes(4);
  });
});

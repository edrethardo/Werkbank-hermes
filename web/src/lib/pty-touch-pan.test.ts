// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { installPtyTouchPan, type PtyPanTerminal } from "./pty-touch-pan";

function makeTerm(): PtyPanTerminal {
  return {
    rows: 24,
    buffer: { active: { viewportY: 0 } },
    attachCustomWheelEventHandler: () => {},
    scrollLines: () => {},
    scrollToLine: () => {},
  };
}

function touch(id: number, clientY: number): Touch {
  return { identifier: id, clientY } as Touch;
}

describe("installPtyTouchPan", () => {
  it("does not call preventDefault on a stationary tap (sub-pixel jitter)", () => {
    const term = makeTerm();
    const host = document.createElement("div");
    const touchRoot = document.createElement("div");
    installPtyTouchPan(term, host, touchRoot, { coarsePointer: true });

    touchRoot.dispatchEvent(
      new TouchEvent("touchstart", { touches: [touch(1, 100)] as unknown as Touch[] }),
    );

    const move = new TouchEvent("touchmove", {
      touches: [touch(1, 101)] as unknown as Touch[],
      cancelable: true,
    });
    touchRoot.dispatchEvent(move);
    expect(move.defaultPrevented).toBe(false);
  });

  it("calls preventDefault once the finger has actually moved a pan-worthy distance", () => {
    const term = makeTerm();
    const host = document.createElement("div");
    const touchRoot = document.createElement("div");
    installPtyTouchPan(term, host, touchRoot, { coarsePointer: true });

    touchRoot.dispatchEvent(
      new TouchEvent("touchstart", { touches: [touch(1, 100)] as unknown as Touch[] }),
    );

    const move = new TouchEvent("touchmove", {
      touches: [touch(1, 160)] as unknown as Touch[],
      cancelable: true,
    });
    touchRoot.dispatchEvent(move);
    expect(move.defaultPrevented).toBe(true);
  });

  it("dispatches a synthetic click on a stationary tap (WebKit drops its own under touch-action:none)", () => {
    const term = makeTerm();
    const host = document.createElement("div");
    const touchRoot = document.createElement("div");
    installPtyTouchPan(term, host, touchRoot, { coarsePointer: true });

    let clicked: MouseEvent | null = null;
    let moved = false;
    touchRoot.addEventListener("mousemove", () => {
      moved = true;
    });
    touchRoot.addEventListener("click", (ev) => {
      clicked = ev as MouseEvent;
    });

    touchRoot.dispatchEvent(
      new TouchEvent("touchstart", {
        touches: [{ identifier: 1, clientX: 42, clientY: 100 } as unknown as Touch],
      }),
    );
    touchRoot.dispatchEvent(new TouchEvent("touchend", { touches: [] }));

    expect(clicked).not.toBeNull();
    expect(moved).toBe(true);
    expect((clicked as unknown as MouseEvent).clientX).toBe(42);
    expect((clicked as unknown as MouseEvent).clientY).toBe(100);
  });

  it("does not dispatch a synthetic click after an actual pan", () => {
    const term = makeTerm();
    const host = document.createElement("div");
    const touchRoot = document.createElement("div");
    installPtyTouchPan(term, host, touchRoot, { coarsePointer: true });

    let clicked = false;
    touchRoot.addEventListener("click", () => {
      clicked = true;
    });

    touchRoot.dispatchEvent(
      new TouchEvent("touchstart", {
        touches: [{ identifier: 1, clientX: 42, clientY: 100 } as unknown as Touch],
      }),
    );
    touchRoot.dispatchEvent(
      new TouchEvent("touchmove", {
        touches: [{ identifier: 1, clientX: 42, clientY: 160 } as unknown as Touch],
        cancelable: true,
      }),
    );
    touchRoot.dispatchEvent(new TouchEvent("touchend", { touches: [] }));

    expect(clicked).toBe(false);
  });
});

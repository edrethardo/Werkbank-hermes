import { describe, expect, it } from "vitest";

import {
  parseSessionControlMessage,
  tabResumeTarget,
  withTabSession,
} from "./pty-tab-session";

describe("pty-tab-session (WB-781)", () => {
  it("parses only the session control frame", () => {
    expect(parseSessionControlMessage('{"type":"session","id":"S1"}')).toBe("S1");
    expect(parseSessionControlMessage('{"type":"resume","id":"S1"}')).toBeNull();
    expect(parseSessionControlMessage('{"type":"session","id":""}')).toBeNull();
    expect(parseSessionControlMessage("\x1b[31mChat unavailable")).toBeNull();
    expect(parseSessionControlMessage("{not json")).toBeNull();
  });

  it("asks for the session the tab showed last, not the one it was opened with", () => {
    expect(tabResumeTarget(new URLSearchParams("resume=A&tab_session=B"))).toBe("B");
    expect(tabResumeTarget(new URLSearchParams("resume=A"))).toBe("A");
    expect(tabResumeTarget(new URLSearchParams("tab_session=B"))).toBe("B");
    expect(tabResumeTarget(new URLSearchParams(""))).toBeNull();
    expect(tabResumeTarget(new URLSearchParams("resume=A&tab_session=B"), true)).toBeNull();
  });

  it("records the hosted session in the URL only when it changes", () => {
    const fresh = withTabSession(new URLSearchParams("profile=p"), "S1");
    expect(fresh?.toString()).toBe("profile=p&tab_session=S1");
    expect(withTabSession(new URLSearchParams("tab_session=S1"), "S1")).toBeNull();
    // Same as ?resume=: the URL already says it.
    expect(withTabSession(new URLSearchParams("resume=S1"), "S1")).toBeNull();
    expect(withTabSession(new URLSearchParams("resume=S1&tab_session=S2"), "S1")?.toString())
      .toBe("resume=S1");
  });
});

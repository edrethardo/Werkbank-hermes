import { describe, expect, it } from "vitest";

import { ptyChatHref, readPhonePointer } from "./pty-chat-links";

describe("pty chat links", () => {
  it("keeps resume and profile on an explicit PTY diagnostic link", () => {
    expect(ptyChatHref("sess-9", "worker")).toBe("/chat?resume=sess-9&profile=worker&pty=1");
  });

  it("omits an empty profile instead of sending a blank scope", () => {
    expect(ptyChatHref("sess-9")).toBe("/chat?resume=sess-9&pty=1");
  });

  it("reports a neutral pointer when there is no window to probe", () => {
    expect(readPhonePointer()).toEqual({
      maxTouchPoints: expect.any(Number),
      pointerCoarse: expect.any(Boolean),
    });
  });
});

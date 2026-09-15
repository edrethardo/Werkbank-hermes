// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";

import {
  CHAT_BREADCRUMB_KEY,
  CHAT_BREADCRUMB_MAX,
  CHAT_BREADCRUMB_RECENT_MS,
  formatChatBreadcrumbBanner,
  readChatBreadcrumbs,
  recordChatBreadcrumb,
} from "./chat-reload-breadcrumb";

function fakeStorage() {
  const map = new Map<string, string>();
  return {
    getItem: (key: string) => map.get(key) ?? null,
    setItem: (key: string, value: string) => void map.set(key, value),
    removeItem: (key: string) => void map.delete(key),
    map,
  };
}

describe("chat reload breadcrumb", () => {
  let storage: ReturnType<typeof fakeStorage>;

  beforeEach(() => {
    storage = fakeStorage();
  });

  it("records a reload cause that survives the reload", () => {
    recordChatBreadcrumb("ws-auth-reload", "code=4401", storage, 1000);
    expect(readChatBreadcrumbs(storage)).toEqual([
      { at: 1000, kind: "ws-auth-reload", detail: "code=4401" },
    ]);
  });

  it("keeps only the most recent entries", () => {
    for (let i = 0; i < CHAT_BREADCRUMB_MAX + 5; i++) {
      recordChatBreadcrumb("ws-close", `code=${i}`, storage, i);
    }
    const entries = readChatBreadcrumbs(storage);
    expect(entries).toHaveLength(CHAT_BREADCRUMB_MAX);
    expect(entries.at(-1)?.detail).toBe(`code=${CHAT_BREADCRUMB_MAX + 4}`);
  });

  it("never throws when storage is blocked", () => {
    const blocked = {
      getItem() { throw new Error("blocked"); },
      setItem() { throw new Error("blocked"); },
      removeItem() { throw new Error("blocked"); },
    };
    expect(() => recordChatBreadcrumb("ws-close", "code=1006", blocked, 1)).not.toThrow();
    expect(readChatBreadcrumbs(blocked)).toEqual([]);
  });

  it("ignores a corrupted payload instead of breaking the page", () => {
    storage.map.set(CHAT_BREADCRUMB_KEY, "{not json");
    expect(readChatBreadcrumbs(storage)).toEqual([]);
  });

  // The whole point: after the page came back, the user must be able to read
  // WHY it came back without a macOS web inspector.
  it("explains a fresh reload cause on the next page load", () => {
    recordChatBreadcrumb("ws-auth-reload", "code=4401", storage, 10_000);
    const banner = formatChatBreadcrumbBanner(readChatBreadcrumbs(storage), 10_500);
    expect(banner).toContain("4401");
    expect(banner).toContain("ws-auth-reload");
  });

  it("stays silent for an old breadcrumb", () => {
    recordChatBreadcrumb("ws-auth-reload", "code=4401", storage, 0);
    expect(
      formatChatBreadcrumbBanner(readChatBreadcrumbs(storage), CHAT_BREADCRUMB_RECENT_MS + 1),
    ).toBeNull();
  });

  it("stays silent when nothing was recorded", () => {
    expect(formatChatBreadcrumbBanner([], 1)).toBeNull();
  });
});

import { describe, expect, it, vi } from "vitest";

import {
  attemptDashboardTokenReloadOnce,
  clearDashboardTokenReloadAttempt,
  maybeReloadForLoopbackWsAuthFailure,
} from "./dashboard-auth-reload";
import { readChatBreadcrumbs } from "./chat-reload-breadcrumb";

function makeStorage() {
  const values = new Map<string, string>();
  return {
    getItem(key: string) {
      return values.get(key) ?? null;
    },
    removeItem(key: string) {
      values.delete(key);
    },
    setItem(key: string, value: string) {
      values.set(key, value);
    },
  };
}

describe("attemptDashboardTokenReloadOnce", () => {
  it("reloads once and latches the attempt", () => {
    const storage = makeStorage();
    const reload = vi.fn();

    expect(attemptDashboardTokenReloadOnce(storage, reload)).toBe(true);
    expect(reload).toHaveBeenCalledTimes(1);

    expect(attemptDashboardTokenReloadOnce(storage, reload)).toBe(false);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("clears the latch when asked", () => {
    const storage = makeStorage();
    const reload = vi.fn();

    expect(attemptDashboardTokenReloadOnce(storage, reload)).toBe(true);
    clearDashboardTokenReloadAttempt(storage);
    expect(attemptDashboardTokenReloadOnce(storage, reload)).toBe(true);
    expect(reload).toHaveBeenCalledTimes(2);
  });
});

describe("maybeReloadForLoopbackWsAuthFailure", () => {
  it("reloads once for loopback 4401 closes", () => {
    const storage = makeStorage();
    const reload = vi.fn();

    expect(
      maybeReloadForLoopbackWsAuthFailure(4401, false, storage, reload),
    ).toBe(true);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("does not reload in gated mode or for other close codes", () => {
    const storage = makeStorage();
    const reload = vi.fn();

    expect(
      maybeReloadForLoopbackWsAuthFailure(4401, true, storage, reload),
    ).toBe(false);
    expect(
      maybeReloadForLoopbackWsAuthFailure(4403, false, storage, reload),
    ).toBe(false);
    expect(reload).not.toHaveBeenCalled();
  });

  // Without this the reload is indistinguishable from a Safari tab discard.
  it("leaves a breadcrumb naming the reload cause", () => {
    const storage = makeStorage();
    maybeReloadForLoopbackWsAuthFailure(4401, false, storage, vi.fn());
    expect(readChatBreadcrumbs(storage)).toEqual([
      expect.objectContaining({ kind: "ws-auth-reload", detail: "code=4401" }),
    ]);
  });

  it("leaves no breadcrumb when it does not reload", () => {
    const storage = makeStorage();
    maybeReloadForLoopbackWsAuthFailure(4403, false, storage, vi.fn());
    expect(readChatBreadcrumbs(storage)).toEqual([]);
  });
});

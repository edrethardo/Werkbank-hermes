import {
  recordChatBreadcrumb,
  type ChatBreadcrumbKind,
} from "./chat-reload-breadcrumb";

type StorageLike = Pick<Storage, "getItem" | "removeItem" | "setItem">;

const TOKEN_RELOAD_STORAGE_KEY = "hermes.tokenReloadAttempted";

function dashboardAuthRequired(): boolean {
  return typeof window !== "undefined" && !!window.__HERMES_AUTH_REQUIRED__;
}

function reloadDashboardWindow(): void {
  if (typeof window !== "undefined") {
    window.location.reload();
  }
}

function dashboardSessionStorage(): StorageLike | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

export function clearDashboardTokenReloadAttempt(
  storage: StorageLike | null = dashboardSessionStorage(),
): void {
  try {
    storage?.removeItem(TOKEN_RELOAD_STORAGE_KEY);
  } catch {
    /* privacy mode / blocked storage — ignore */
  }
}

/**
 * Reload the dashboard once to pick up a fresh session token.
 *
 * `breadcrumb` names the cause: a note is left BEFORE the reload, because on a
 * phone that is the only way to tell a stale-token reload apart from a
 * reconnect replay or a Safari tab discard. Recorded here rather than at each
 * call site so `api.ts` — a file upstream edits often — stays byte-identical
 * to upstream.
 */
export function attemptDashboardTokenReloadOnce(
  storage: StorageLike | null = dashboardSessionStorage(),
  reload: () => void = reloadDashboardWindow,
  breadcrumb: { kind: ChatBreadcrumbKind; detail: string } = {
    kind: "api-auth-reload",
    detail: "",
  },
): boolean {
  let alreadyReloaded = false;
  try {
    alreadyReloaded =
      storage?.getItem(TOKEN_RELOAD_STORAGE_KEY) === "1";
  } catch {
    /* privacy mode / blocked storage — fall through */
  }
  if (alreadyReloaded) {
    return false;
  }

  try {
    storage?.setItem(TOKEN_RELOAD_STORAGE_KEY, "1");
  } catch {
    /* privacy mode / blocked storage — best effort */
  }

  recordChatBreadcrumb(breadcrumb.kind, breadcrumb.detail, storage);

  reload();
  return true;
}

export function maybeReloadForLoopbackWsAuthFailure(
  code: number,
  authRequired = dashboardAuthRequired(),
  storage: StorageLike | null = dashboardSessionStorage(),
  reload: () => void = reloadDashboardWindow,
): boolean {
  if (authRequired || code !== 4401) {
    return false;
  }
  return attemptDashboardTokenReloadOnce(storage, reload, {
    kind: "ws-auth-reload",
    detail: `code=${code}`,
  });
}

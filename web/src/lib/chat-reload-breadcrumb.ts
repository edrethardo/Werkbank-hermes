/**
 * Why did the chat page just come back?
 *
 * On a phone there is no console: a WebSocket-auth reload, a reconnect replay
 * and a Safari tab discard all look identical — "die Seite lädt kaputt neu".
 * The three have different fixes, so the page must say which one happened.
 *
 * Every reload cause the page controls is written to sessionStorage BEFORE the
 * reload, and read back on the next load. A Safari tab discard leaves no
 * breadcrumb at all — that absence is itself the diagnosis.
 */

export const CHAT_BREADCRUMB_KEY = "hermes.chatReloadBreadcrumbs";
export const CHAT_BREADCRUMB_MAX = 10;
/** Older than this and the breadcrumb belongs to an earlier visit. */
export const CHAT_BREADCRUMB_RECENT_MS = 60_000;

export type ChatBreadcrumbKind =
  | "ws-auth-reload"
  | "ws-close"
  | "pty-reconnect"
  | "api-auth-reload";

export interface ChatBreadcrumb {
  at: number;
  kind: ChatBreadcrumbKind;
  detail: string;
}

type StorageLike = Pick<Storage, "getItem" | "removeItem" | "setItem">;

function defaultStorage(): StorageLike | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

function isBreadcrumb(value: unknown): value is ChatBreadcrumb {
  if (!value || typeof value !== "object") return false;
  const entry = value as Partial<ChatBreadcrumb>;
  return (
    typeof entry.at === "number" &&
    typeof entry.kind === "string" &&
    typeof entry.detail === "string"
  );
}

export function readChatBreadcrumbs(
  storage: StorageLike | null = defaultStorage(),
): ChatBreadcrumb[] {
  let raw: string | null = null;
  try {
    raw = storage?.getItem(CHAT_BREADCRUMB_KEY) ?? null;
  } catch {
    return [];
  }
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(isBreadcrumb);
  } catch {
    // A corrupted payload must never break the chat page.
    return [];
  }
}

export function recordChatBreadcrumb(
  kind: ChatBreadcrumbKind,
  detail: string,
  storage: StorageLike | null = defaultStorage(),
  now: number = Date.now(),
): void {
  const entries = readChatBreadcrumbs(storage);
  entries.push({ at: now, kind, detail });
  const kept = entries.slice(-CHAT_BREADCRUMB_MAX);
  try {
    storage?.setItem(CHAT_BREADCRUMB_KEY, JSON.stringify(kept));
  } catch {
    /* privacy mode / blocked storage — best effort */
  }
}

export function clearChatBreadcrumbs(
  storage: StorageLike | null = defaultStorage(),
): void {
  try {
    storage?.removeItem(CHAT_BREADCRUMB_KEY);
  } catch {
    /* ignore */
  }
}

/** A short line naming the most recent cause, or null when there is none fresh. */
export function formatChatBreadcrumbBanner(
  entries: ChatBreadcrumb[],
  now: number = Date.now(),
): string | null {
  const last = entries.at(-1);
  if (!last) return null;
  if (now - last.at > CHAT_BREADCRUMB_RECENT_MS) return null;
  return `Chat reloaded: ${last.kind} (${last.detail}).`;
}

/**
 * The banner the chat page starts with.
 *
 * Two unrelated things want that slot on first paint: a missing session token
 * (the page was opened outside `hermes dashboard`), and the reason the page
 * just reloaded itself. The token complaint wins — without it nothing works.
 * In gated (OAuth) mode the server intentionally omits the token, so a missing
 * token there is expected, not an error.
 */
export function initialChatBanner(): string | null {
  if (
    typeof window !== "undefined" &&
    !window.__HERMES_SESSION_TOKEN__ &&
    !window.__HERMES_AUTH_REQUIRED__
  ) {
    return "Session token unavailable. Open this page through `hermes dashboard`, not directly.";
  }
  // The page may have just reloaded itself. Say why, so a phone user can
  // report the actual cause instead of "die Seite lädt kaputt neu".
  return formatChatBreadcrumbBanner(readChatBreadcrumbs());
}

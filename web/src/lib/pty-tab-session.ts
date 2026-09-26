/**
 * Which Hermes session THIS /chat tab shows, kept in the tab's own URL (WB-781).
 *
 * A browser restart with "restore tabs" brings every tab's URL back, but the
 * keep-alive `?attach=` token lives in sessionStorage, which browsers do not
 * reliably restore — and a tab opened as a plain `/chat` has no `?resume=` at
 * all. Such a tab came back as a brand-new chat, or as a second TUI for its old
 * session that the first TUI's lease refused ("open in another Hermes window").
 *
 * The PTY tells the tab which session it hosts (`{"type":"session","id":…}` text
 * frame, sent once the client opts in with `session_frames=1`). The tab records
 * it as `?tab_session=` via `history.replaceState`, so a restored tab asks for
 * exactly that session again and the server can hand it the orphaned PTY.
 */

export const TAB_SESSION_PARAM = "tab_session";

/** Session id from a `{"type":"session","id":…}` control frame, else null. */
export function parseSessionControlMessage(data: string): string | null {
  if (!data.startsWith("{")) return null;
  try {
    const parsed = JSON.parse(data);
    if (
      parsed &&
      typeof parsed === "object" &&
      parsed.type === "session" &&
      typeof parsed.id === "string" &&
      parsed.id
    ) {
      return parsed.id;
    }
  } catch {
    /* not JSON — an ANSI banner or other plain-text frame */
  }
  return null;
}

/**
 * The session this tab asks the PTY for: the one it last showed wins over the
 * `?resume=` it was opened with (the user may have switched sessions inside the
 * TUI since). A forced-fresh start asks for none.
 */
export function tabResumeTarget(
  search: URLSearchParams,
  forceFresh = false,
): string | null {
  if (forceFresh) return null;
  return search.get(TAB_SESSION_PARAM) || search.get("resume") || null;
}

/**
 * `search` with `?tab_session=` pointing at `sessionId` — or null when nothing
 * changes. Equal to `?resume=` means the URL already says it; drop the extra.
 */
export function withTabSession(
  search: URLSearchParams,
  sessionId: string | null,
): URLSearchParams | null {
  const current = search.get(TAB_SESSION_PARAM);
  const wanted = sessionId && sessionId !== search.get("resume") ? sessionId : null;
  if ((current || null) === wanted) return null;
  const next = new URLSearchParams(search);
  if (wanted) next.set(TAB_SESSION_PARAM, wanted);
  else next.delete(TAB_SESSION_PARAM);
  return next;
}

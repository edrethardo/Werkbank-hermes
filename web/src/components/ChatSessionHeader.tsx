/**
 * One line above the embedded terminal naming the session on screen.
 *
 * On a phone the terminal fills the viewport with no window title and no URL
 * bar to read, so "which session am I in" has no answer. The id is rendered
 * `select-all` so a single tap copies it into a bug report.
 *
 * Renders nothing when neither a title nor a resume id is known — a blank
 * strip above the terminal is worse than no strip.
 */

export function ChatSessionHeader({
  resumeId,
  title,
}: {
  /** The `?resume=` id currently on the URL, if any. */
  resumeId: string | null;
  /** The resolved session title, if the gateway already reported one. */
  title: string | null;
}) {
  if (!title && !resumeId) return null;
  return (
    <div className="mb-1 flex min-w-0 items-center gap-2 px-1 text-xs text-text-secondary">
      <span className="truncate">{title || "Chat"}</span>
      {resumeId && <code className="shrink-0 select-all">{resumeId}</code>}
    </div>
  );
}

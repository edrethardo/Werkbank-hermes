// Werkbank hosts this /chat surface in a frame and may ask the PTY to run one
// of a fixed set of foreign programs instead of Hermes' own TUI (see
// hermes_cli/werkbank_programs.py). The terminal, its input adapter and every
// improvement made to them are shared; only the child process differs.
//
// Kept in a file of its own so an upstream rebase of ChatPage never conflicts.

const PROGRAMS = new Set(["claude-code"]);

export function ptyWerkbankParams(
  search: URLSearchParams, cols?: number, rows?: number,
): Record<string, string> {
  const program = (search.get("program") || "").trim();
  if (!PROGRAMS.has(program)) return {};
  const out: Record<string, string> = { program };
  const project = (search.get("project") || "").trim();
  if (project.startsWith("/")) out.project = project;
  // Spawn at the size the browser already has. A foreign TUI redraws its whole
  // intro on SIGWINCH and appends the redraw, so starting at 80x24 and being
  // resized a moment later leaves two or three stacked copies of the banner.
  if (Number.isFinite(cols) && Number.isFinite(rows) && (cols as number) > 0 && (rows as number) > 0) {
    out.cols = String(Math.trunc(cols as number));
    out.rows = String(Math.trunc(rows as number));
  }
  return out;
}

// Distinct channel per program+project: a Claude Code tab must not share the
// sidebar channel or the keep-alive identity of a Hermes chat.
export function ptyWerkbankChannelKey(search: URLSearchParams): string {
  const p = ptyWerkbankParams(search);
  return `${p.program ?? ""}\0${p.project ?? ""}`;
}

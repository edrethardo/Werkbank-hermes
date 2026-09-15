"""Allowlisted foreign programs the dashboard's chat PTY may host.

Werkbank (the ticket board at https://github.com/edrethardo/werkbank) shows this
dashboard's ``/chat`` page in a frame and drives it through ``/api/pty``.  It
wants the SAME terminal for Claude Code that Hermes' own TUI gets, so that every
improvement made to the chat surface — dictation, paste, caret, one-finger
scroll — applies to both without a line of it being copied.

This module is the whole of that contract, deliberately in a file of its own so
an upstream rebase never conflicts with it.  Three rules:

* The program is chosen from a FIXED table, never from the request.  A
  ``?program=`` that is not a key here is refused.
* The working directory is NOT taken from the request either.  Werkbank's own
  launcher decides it, from the project list in Werkbank's config.
* The child gets a scrubbed environment.  Hermes' provider credentials are not
  handed to a foreign agent.

``project`` therefore travels as an opaque string that only Werkbank's launcher
interprets, and the launcher refuses anything it has not registered.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

__all__ = ["PROGRAMS", "UnknownProgramError", "is_external", "resolve_external_program",
           "spawn_size"]

# Overridable so a differently installed Werkbank does not need a patch.
_LAUNCHER_ENV = "WERKBANK_TUI_LAUNCHER"
_DEFAULT_LAUNCHER = "/data/projects/agent_ticket/scripts/werkbank-claude-tui.py"

PROGRAMS = {
    "claude-code": {
        "launcher_env": _LAUNCHER_ENV,
        "launcher_default": _DEFAULT_LAUNCHER,
        "project_env": "WERKBANK_PROJECT",
        "title": "Claude Code",
    },
}

# A project reference is a plain absolute POSIX path.  The launcher does the
# real check against its registered projects; this only keeps obvious rubbish
# (NUL, newlines, shell padding, absurd length) out of a child environment.
_MAX_PROJECT = 4096


class UnknownProgramError(ValueError):
    """The request named a program that is not in the table."""


def is_external(program: Optional[str]) -> bool:
    return bool((program or "").strip())


def _clean_project(project: Optional[str]) -> str:
    value = (project or "").strip()
    if not value:
        return ""
    if len(value) > _MAX_PROJECT or not value.startswith("/"):
        raise UnknownProgramError("project must be an absolute path")
    if any(ch in value for ch in ("\0", "\n", "\r")):
        raise UnknownProgramError("project contains control characters")
    return value


def resolve_external_program(program: Optional[str], project: Optional[str] = None):
    """Return ``(argv, cwd, env)`` for an allowlisted foreign program.

    Mirrors the return shape of ``_resolve_chat_argv`` so the PTY route, its
    keep-alive registry and its resize handling stay completely unaware that
    the child is not Hermes.
    """
    key = (program or "").strip()
    spec = PROGRAMS.get(key)
    if spec is None:
        raise UnknownProgramError(f"unknown program {key!r}")
    launcher = os.environ.get(spec["launcher_env"]) or spec["launcher_default"]
    if not os.path.isfile(launcher):
        raise FileNotFoundError(
            f"{spec['title']}: launcher {launcher} is not installed on this host")

    from tools.environments.local import build_subprocess_env
    env = build_subprocess_env(scrub_secrets=True)
    # The Ink TUI reads these; a foreign program must not be told to imitate it.
    for name in [k for k in env if k.startswith("HERMES_TUI_")]:
        env.pop(name, None)
    env.pop("HERMES_PTY_HOST", None)
    cleaned = _clean_project(project)
    if cleaned:
        env[spec["project_env"]] = cleaned
    env.setdefault("COLORTERM", "truecolor")
    # cwd stays None on purpose: the launcher chooses it from its own project
    # register, so a request can never point the child at a directory.
    return [sys.executable, launcher], None, env


# The browser knows its terminal size before it connects. Spawning at 80x24 and
# resizing a moment later makes a foreign TUI redraw its whole intro and append
# the redraw, so the banner ends up stacked two or three times. Starting at the
# real size removes the resize entirely.
_DEFAULT_COLS, _DEFAULT_ROWS = 80, 24
_MAX_COLS, _MAX_ROWS = 2000, 1000


def spawn_size(cols, rows):
    """Clamp a requested terminal size, falling back to 80x24 on nonsense."""
    def one(value, default, maximum):
        try:
            n = int(str(value).strip())
        except (TypeError, ValueError):
            return default
        return n if 1 <= n <= maximum else default
    return one(cols, _DEFAULT_COLS, _MAX_COLS), one(rows, _DEFAULT_ROWS, _MAX_ROWS)

"""Foreign terminal programs a plugin may host in the dashboard's chat PTY.

The ``/chat`` page is a real terminal: dictation, paste, caret sync, one-finger
scroll, keyboard reveal.  A plugin that drives some other terminal program
(a coding agent, a build monitor, a board's own launcher) wants THAT surface
rather than a second, worse copy of it — the same argument
``dashboard_pages.py`` makes for HTTP.

``ctx.register_terminal_program(...)`` puts a program in this registry; the PTY
route resolves ``?program=<name>`` against it and stays completely unaware that
the child is not Hermes.

What the registry guarantees, so that every plugin inherits it instead of
re-deriving it:

* **The program comes from the registry, never from the request.** A
  ``?program=`` that is not a registered name is refused.
* **The working directory is not taken from the request either.** The resolver
  chooses it. A request can never point a child at a directory.
* **The child gets a scrubbed environment.** Hermes' provider credentials are
  not handed to a foreign program, and the ``HERMES_TUI_*`` variables that tell
  a process to imitate the Ink TUI are removed.
* **``argument`` travels as an opaque string** that only the plugin's resolver
  interprets. The registry keeps obvious rubbish (NUL, newlines, absurd length)
  out of a child environment; the resolver does the real check.

This file is deliberately free of any particular plugin's knowledge: no paths,
no program names, no environment-variable conventions.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "TerminalProgram", "ProgramNameError", "UnknownProgramError",
    "register_program", "unregister_program", "registered_programs",
    "is_external", "resolve_external_program", "clean_argument", "spawn_size",
]

#: A program name travels in a query string and is used as a registry key, so
#: keep it to the shape that is unambiguous in both: lowercase, digits, dash.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

#: An opaque argument for the resolver. Only length and control characters are
#: checked here — meaning belongs to the plugin.
_MAX_ARGUMENT = 4096


class ProgramNameError(ValueError):
    """The program name is malformed or already registered by another plugin."""


class UnknownProgramError(ValueError):
    """The request named a program that is not registered."""


@dataclass(frozen=True)
class TerminalProgram:
    """One hostable program.

    ``resolve(argument)`` returns ``(argv, cwd, env)`` exactly like Hermes' own
    chat resolver, so the PTY route, its keep-alive registry and its resize
    handling need no special case. It may raise ``FileNotFoundError`` when the
    program is not installed on this host; the route turns that into a readable
    message rather than a stack trace.
    """

    name: str
    title: str
    resolve: Callable[[str], Tuple[List[str], Optional[str], Dict[str, str]]]
    plugin: str = ""


_lock = threading.Lock()
_programs: Dict[str, TerminalProgram] = {}


def register_program(program: TerminalProgram) -> None:
    """Add a program to the registry (raises ``ProgramNameError`` on a bad or taken name)."""
    if not isinstance(program, TerminalProgram):
        raise TypeError("program must be a TerminalProgram")
    name = (program.name or "").strip()
    if not _NAME_RE.match(name):
        raise ProgramNameError(
            f"program name {program.name!r} must be lowercase letters, digits or dashes")
    if not callable(program.resolve):
        raise TypeError(f"program {name!r} has no callable resolve()")
    with _lock:
        taken = _programs.get(name)
        # Same plugin re-registering (a reload) replaces its own entry; a
        # DIFFERENT plugin claiming a live name is refused, so a plugin cannot
        # quietly take over the terminal another one published.
        if taken is not None and taken.plugin != program.plugin:
            raise ProgramNameError(
                f"program {name!r} is already registered by plugin {taken.plugin!r}")
        _programs[name] = program


def unregister_program(name: str, expected: Optional[TerminalProgram] = None) -> None:
    """Remove a program. Identity-conditional: an older generation cannot evict a newer entry."""
    with _lock:
        current = _programs.get(name)
        if current is None:
            return
        if expected is not None and current is not expected:
            return
        _programs.pop(name, None)


def registered_programs() -> Dict[str, TerminalProgram]:
    """A snapshot of the registry (name -> program)."""
    with _lock:
        return dict(_programs)


def is_external(program: Optional[str]) -> bool:
    """Does this request ask for a foreign program at all?"""
    return bool((program or "").strip())


def clean_argument(argument: Optional[str]) -> str:
    """Reject an argument that could not be a sane opaque reference.

    Deliberately shallow: the plugin's resolver decides what the string MEANS
    and whether it is allowed. This only keeps things out of a child
    environment that no legitimate reference contains.

    The control-character check runs BEFORE ``strip()`` on purpose. Stripping
    first would silently swallow a trailing ``\\r`` or ``\\n`` — the exact shape
    a CRLF-smuggling attempt has — and report the value as clean.
    """
    raw = argument or ""
    if any(ch in raw for ch in ("\0", "\n", "\r")):
        raise UnknownProgramError("argument contains control characters")
    value = raw.strip()
    if not value:
        return ""
    if len(value) > _MAX_ARGUMENT:
        raise UnknownProgramError("argument is too long")
    return value


def scrubbed_child_env() -> Dict[str, str]:
    """The environment a foreign program starts with.

    Offered here so every resolver inherits the same scrub instead of each
    plugin remembering which variables matter.
    """
    from tools.environments.local import build_subprocess_env
    env = build_subprocess_env(scrub_secrets=True)
    # The Ink TUI reads these; a foreign program must not be told to imitate it.
    for name in [k for k in env if k.startswith("HERMES_TUI_")]:
        env.pop(name, None)
    env.pop("HERMES_PTY_HOST", None)
    env.setdefault("COLORTERM", "truecolor")
    return env


def resolve_external_program(program: Optional[str], argument: Optional[str] = None):
    """Return ``(argv, cwd, env)`` for a registered program.

    Mirrors the return shape of ``_resolve_chat_argv``.
    """
    key = (program or "").strip()
    entry = registered_programs().get(key)
    if entry is None:
        raise UnknownProgramError(f"unknown program {key!r}")
    return entry.resolve(clean_argument(argument))


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

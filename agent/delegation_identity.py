"""WB-1338 (fork: werkbank): a delegated child's identity in its subprocess environment.

A child (``delegate_task``) runs its terminal and ``execute_code`` processes with ``HERMES_SESSION_ID``
= its OWN session id — nothing in that environment says which delegation, which task, or which
parent chat it belongs to. Tools that start compute for the child (the Werkbank's ``bin/wb compute
melden``, ``nebius_vm.sh``, ``gpu_queue.sh``) need exactly that to report „who steers this VM“
without relying on the model to copy an id from its prompt.

While a child runs, :func:`bound` sets a ContextVar; :func:`apply` (called from
``delegation_context.delegated_child_subprocess_env`` — every spawn surface goes through it) writes:

* ``HERMES_DELEGATION_ID`` — the live-transcript folder (``cache/delegation/live/<id>``),
* ``HERMES_DELEGATION_TASK`` — the task index (``task-<n>.log``),
* ``HERMES_PARENT_SESSION_ID`` — the session that delegated (the chat one writes to).

Outside a bound child the three names are REMOVED from the env (a child's value must never leak
into a sibling or into the parent's own spawns via an inherited environment).
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, MutableMapping, Optional

KEYS = ("HERMES_DELEGATION_ID", "HERMES_DELEGATION_TASK", "HERMES_PARENT_SESSION_ID")
_DELEG_RE = re.compile(r"deleg_[0-9a-f]{6,32}")
_SESSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")

_IDENTITY: ContextVar[Optional[Dict[str, str]]] = ContextVar("hermes_delegation_identity", default=None)


def identity_of(child: Any, task_index: Any) -> Optional[Dict[str, str]]:
    """The three values for *child*, or None when its delegation id is not a live-transcript id."""
    deleg = getattr(child, "_delegation_id", None)
    if not isinstance(deleg, str) or not _DELEG_RE.fullmatch(deleg):
        return None
    if isinstance(task_index, bool) or not isinstance(task_index, int) or task_index < 0:
        return None
    out = {"HERMES_DELEGATION_ID": deleg, "HERMES_DELEGATION_TASK": str(task_index)}
    parent = getattr(child, "_parent_session_id", None)
    if isinstance(parent, str) and _SESSION_RE.fullmatch(parent):
        out["HERMES_PARENT_SESSION_ID"] = parent
    return out


@contextmanager
def bound(child: Any, task_index: Any) -> Iterator[None]:
    token = _IDENTITY.set(identity_of(child, task_index))
    try:
        yield
    finally:
        _IDENTITY.reset(token)


def current() -> Optional[Dict[str, str]]:
    return _IDENTITY.get()


def apply(env: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """Set (bound child) or strip (anything else) the three names in *env*; returns *env*."""
    for key in KEYS:
        env.pop(key, None)
    ident = _IDENTITY.get()
    if ident:
        env.update(ident)
    return env

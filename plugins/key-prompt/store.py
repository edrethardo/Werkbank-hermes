"""One-shot key requests shared by the agent process (gateway/CLI) and the dashboard process.

A request is a small JSON record under ``<hermes root>/key-prompt/requests/<id>.json`` (dir 0700,
file 0600). It never holds the key: the dashboard stores the key straight into the credential pool
and only writes the outcome (``stored``/``cancelled``) and the entry label back here. Both
processes run as the same user and share the Hermes root regardless of profile, so a file is the
simplest hand-off that needs no live socket and survives neither side waiting.

Single use is an ``O_EXCL`` claim file next to the record: whoever creates ``<id>.claim`` first
is the only one allowed to finish the request.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Optional

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")

DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 900
#: Creation limits: open requests per session, and requests per rolling hour overall.
MAX_PENDING_PER_SESSION = 3
MAX_PER_HOUR = 12
#: Finished records are kept this long for ``api_key_request_status``, then swept.
KEEP_FINISHED = 24 * 3600

PENDING, STORED, CANCELLED, EXPIRED = "pending", "stored", "cancelled", "expired"


class RateLimited(Exception):
    pass


def _now() -> float:
    return time.time()


def _root() -> Path:
    from hermes_constants import get_default_hermes_root
    return Path(get_default_hermes_root())


def _private_dir(path: Path) -> Path:
    """Create ``path`` 0700 and refuse anything this user does not own (no symlink tricks)."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise PermissionError(f"{path} is not a directory owned by this user")
    os.chmod(path, 0o700)
    return path


def requests_dir() -> Path:
    _private_dir(_root() / "key-prompt")
    return _private_dir(_root() / "key-prompt" / "requests")


def audit_path() -> Path:
    return _private_dir(_root() / "key-prompt") / "audit.log"


def valid_request_id(rid) -> bool:
    return isinstance(rid, str) and bool(_ID_RE.fullmatch(rid))


def request_path(rid: str) -> Path:
    if not valid_request_id(rid):
        raise ValueError("invalid request id")
    return requests_dir() / f"{rid}.json"


def _write_private(path: Path, data: dict) -> None:
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _read(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def audit(event: str, rec: dict, **extra) -> None:
    """Append one value-free line. Only an id prefix is logged: the full id is a live capability."""
    line = {"ts": int(_now()), "event": event, "request": str(rec.get("id", ""))[:8],
            "provider": rec.get("provider", ""), "label": rec.get("label", ""),
            "session": str(rec.get("session", ""))[:12], **extra}
    try:
        path = audit_path()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass


def effective_status(rec: dict) -> str:
    status = rec.get("status") or PENDING
    if status == PENDING and _now() >= float(rec.get("expires", 0)):
        return EXPIRED
    return status


def _all_records() -> list[dict]:
    out = []
    for path in requests_dir().glob("*.json"):
        rec = _read(path)
        if rec:
            out.append(rec)
    return out


def sweep() -> None:
    """Drop records (and their claims) that are long finished or long expired."""
    cutoff = _now() - KEEP_FINISHED
    for path in requests_dir().glob("*.json"):
        rec = _read(path)
        if rec is None or float(rec.get("expires", 0)) < cutoff:
            for p in (path, path.with_suffix(".claim")):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass


def create(provider: str, *, session: str, label: str = "", purpose: str = "",
           requester_profile: str = "", timeout: int = DEFAULT_TIMEOUT) -> dict:
    """New pending request; raises :class:`RateLimited` past the creation limits."""
    sweep()
    now = _now()
    records = _all_records()
    pending_here = [r for r in records
                    if r.get("session") == session and effective_status(r) == PENDING]
    if len(pending_here) >= MAX_PENDING_PER_SESSION:
        raise RateLimited(f"{len(pending_here)} key requests of this session are still open")
    if sum(1 for r in records if float(r.get("created", 0)) > now - 3600) >= MAX_PER_HOUR:
        raise RateLimited("too many key requests in the last hour")
    timeout = max(30, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    rec = {
        "id": secrets.token_urlsafe(24), "csrf": secrets.token_urlsafe(24),
        "provider": provider, "label": label, "purpose": purpose, "session": session,
        "requester_profile": requester_profile, "created": now, "expires": now + timeout,
        "status": PENDING, "bad_attempts": 0,
    }
    _write_private(request_path(rec["id"]), rec)
    audit("created", rec, requester_profile=requester_profile)
    return rec


def load(rid: str) -> Optional[dict]:
    if not valid_request_id(rid):
        return None
    try:
        return _read(request_path(rid))
    except (OSError, ValueError):
        return None


def update(rid: str, **fields) -> Optional[dict]:
    rec = load(rid)
    if rec is None:
        return None
    rec.update(fields)
    _write_private(request_path(rid), rec)
    return rec


def claim(rid: str) -> Optional[dict]:
    """Take the request for good (first caller wins). ``None`` when gone, finished or expired."""
    rec = load(rid)
    if rec is None or effective_status(rec) != PENDING:
        return None
    try:
        fd = os.open(request_path(rid).with_suffix(".claim"),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return None
    os.close(fd)
    return rec


def release(rid: str) -> None:
    """Give an unfinished claim back (the submit failed validation, request stays usable)."""
    try:
        request_path(rid).with_suffix(".claim").unlink()
    except (FileNotFoundError, ValueError):
        pass


def finish(rid: str, status: str, **fields) -> Optional[dict]:
    rec = update(rid, status=status, finished=_now(), csrf="", **fields)
    if rec is not None:
        audit(status, rec, **{k: v for k, v in fields.items() if k in ("profile", "reason")})
    return rec

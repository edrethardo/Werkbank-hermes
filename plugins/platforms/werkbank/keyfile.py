"""The shared secret between this plugin and the Werkbank board.

``<profile home>/plugin-data/werkbank/key``: directory 0700, file 0600, created once on the first
start and reused afterwards (the board reads it from there — same user, same machine). A key
shorter than ``MIN_KEY_CHARS`` is replaced by a new one. The key never appears in a log line or an HTTP answer.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

KEY_BYTES = 32
MIN_KEY_CHARS = 32          # a shorter (hand-written, truncated) key is replaced by a new one


def key_dir(home: Path) -> Path:
    return Path(home) / "plugin-data" / "werkbank"


def key_path(home: Path) -> Path:
    return key_dir(home) / "key"


class KeyFileError(RuntimeError):
    """The key file exists but is not a usable regular file (no text of it in the message)."""


def _tighten_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise KeyFileError(f"{path} is not a directory")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(path, 0o700)


def _read(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise KeyFileError(f"{path} is not a regular file")
        if stat.S_IMODE(info.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as handle:
            return handle.read().strip()
    finally:
        os.close(fd)


def _usable(key: str) -> bool:
    return len(key) >= MIN_KEY_CHARS and not any(c.isspace() for c in key)


def ensure_key(home: Path) -> str:
    """The key of this profile; created (0600, in a 0700 directory) when there is none yet.

    Race-safe: two starts at the same moment (the gateway loads the plugin from more than one
    scope) must end with ONE key — the one on disk. A new key is published with ``os.link``,
    which fails when another start was first; then that key is read and used. Only a present
    but unusable key is replaced (``os.replace``)."""
    directory = key_dir(home)
    _tighten_dir(directory)
    path = key_path(home)
    try:
        key = _read(path)
        present = True
    except FileNotFoundError:
        key, present = "", False
    if _usable(key):
        return key
    key = secrets.token_urlsafe(KEY_BYTES)
    tmp = directory / f".key.{os.getpid()}.{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, (key + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        if present:
            os.replace(tmp, path)
            return key
        try:
            os.link(tmp, path)
            return key
        except FileExistsError:
            other = _read(path)
            if _usable(other):
                return other
            os.replace(tmp, path)
            return key
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass

"""Which SessionDB user row is which Werkbank message (``client_message_id``).

The stream names a ``user.message`` by the sender's own id (``client_message_id``, else the
platform message id — ``gateway/session_events._message_ref``). The SessionDB row of a Telegram
message carries that platform id (``platform_message_id``); the row of a message the board sent
carries none, because an injected event has no platform id. So the plugin remembers each accepted
board message as ``(session_key, length, HMAC-SHA256 of the text keyed with the plugin key)`` → ``client_message_id`` in
``<profile home>/plugin-data/werkbank/messages.json`` (0600, hashes only — no text) and a later
snapshot gives the matching row that id. Survives a plugin restart; the oldest entries go first.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_ENTRIES = 4000


def _digest(text: str, secret: bytes) -> str:
    # Keyed (HMAC-SHA256 with the plugin key): a short text cannot be guessed from the file alone.
    return hmac.new(secret, text.strip().encode("utf-8"), hashlib.sha256).hexdigest()


class IdMap:
    def __init__(self, path: Optional[Path] = None, secret: str = "") -> None:
        self.path = Path(path) if path else None
        self._secret = (secret or secrets.token_hex(32)).encode("utf-8")
        self._lock = threading.Lock()
        self._entries: List[Dict[str, Any]] = self._load()

    def _load(self) -> List[Dict[str, Any]]:
        if self.path is None:
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        rows = data.get("entries") if isinstance(data, dict) else None
        return [r for r in rows or [] if isinstance(r, dict) and {"k", "h", "n", "c"} <= set(r)]

    def _save(self) -> None:
        if self.path is None:
            return
        tmp = self.path.with_name(f".messages.{os.getpid()}.{secrets.token_hex(4)}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, json.dumps({"v": 1, "entries": self._entries}).encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, self.path)

    def remember(self, session_key: str, text: str, client_message_id: str) -> None:
        body = text.strip()
        with self._lock:
            self._entries.append({"k": session_key, "h": _digest(body, self._secret), "n": len(body), "c": client_message_id})
            del self._entries[:-MAX_ENTRIES]
            try:
                self._save()
            except OSError:
                pass                                   # the in-memory map still serves this process

    def forget(self, session_key: str, client_message_id: str) -> None:
        with self._lock:
            self._entries = [e for e in self._entries
                             if not (e["k"] == session_key and e["c"] == client_message_id)]
            try:
                self._save()
            except OSError:
                pass

    def assign(self, session_key: str, user_texts: List[str]) -> List[Optional[str]]:
        """``client_message_id`` per user row text (oldest row first), ``None`` where none fits.

        A row may carry a prefix the gateway added, so its tail of the remembered length counts.
        Each remembered message names at most one row; the same text twice pairs up in order."""
        with self._lock:
            pool = [e for e in self._entries if e["k"] == session_key]
        used = set()
        out: List[Optional[str]] = []
        for text in user_texts:
            body = (text or "").strip()
            hit = None
            for index, entry in enumerate(pool):
                if index in used or entry["n"] > len(body):
                    continue
                if hmac.compare_digest(_digest(body[len(body) - entry["n"]:], self._secret), entry["h"]):
                    hit = index
                    break
            if hit is None:
                out.append(None)
            else:
                used.add(hit)
                out.append(pool[hit]["c"])
        return out

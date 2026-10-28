"""WB-1321: the background workshop for the outbox's playback versions (``outbox.py``).

One worker thread, ONE ffmpeg job at a time, never in a request thread. Every job runs under
``nice -n 15`` and ``ionice -c 3`` (when present), with a time limit, ``umask 077`` and writes to a
``.part`` file that is renamed only when ffmpeg succeeded — a reader never sees a half file.

Without ``ffmpeg``/``ffprobe`` on the PATH nothing is converted (``available`` is False); the outbox
then serves originals only and says so (``grund``).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("plugins.platforms.werkbank")

JOB_TIMEOUT_S = 900          # one ffmpeg run (a 50 MB video at veryfast stays far below)
PROBE_TIMEOUT_S = 30
THREADS = "2"                # ffmpeg threads: the gateway machine keeps its cores


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


class Werkstatt:
    """``submit(fn)`` queues *fn* (it calls :meth:`ffmpeg` / :meth:`probe`); ``synchron=True``
    (tests) runs it at once in the caller's thread."""

    def __init__(self, *, ffmpeg: Optional[str] = "auto", ffprobe: Optional[str] = "auto",
                 synchron: bool = False, timeout: float = JOB_TIMEOUT_S) -> None:
        self.ffmpeg_bin = _which("ffmpeg") if ffmpeg == "auto" else ffmpeg
        self.ffprobe_bin = _which("ffprobe") if ffprobe == "auto" else ffprobe
        self.synchron = synchron
        self.timeout = timeout
        self._queue: "queue.PriorityQueue" = queue.PriorityQueue()
        self._count = 0
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._busy = 0
        self._idle = threading.Condition(self._lock)
        self._prefix: List[str] = []
        nice, ionice = _which("nice"), _which("ionice")
        if nice:
            self._prefix += [nice, "-n", "15"]
        if ionice:
            self._prefix += [ionice, "-c", "3"]

    @property
    def available(self) -> bool:
        return bool(self.ffmpeg_bin and self.ffprobe_bin)

    # -- the queue --------------------------------------------------------------------------------
    def submit(self, job: Callable[[], None], prio: int = 1) -> None:
        """*prio* 0 (previews, probe) runs before 1 (playback versions) before 2 (HLS tiers): a new
        file's preview never waits behind another file's encode."""
        if self.synchron:
            self._run_one(job)
            return
        with self._lock:
            self._busy += 1
            self._count += 1
            ticket = (int(prio), self._count, job)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="werkbank-medien", daemon=True)
                self._thread.start()
        self._queue.put(ticket)

    def _run_one(self, job: Callable[[], None]) -> None:
        try:
            job()
        except Exception:
            logger.info("werkbank: media job failed (%s)", "internal", exc_info=False)

    def _loop(self) -> None:
        while True:
            _prio, _n, job = self._queue.get()
            if job is None:
                return
            self._run_one(job)
            with self._lock:
                self._busy -= 1
                self._idle.notify_all()

    def wait_idle(self, timeout: float = 120.0) -> bool:
        with self._lock:
            return self._idle.wait_for(lambda: self._busy == 0, timeout)

    def stop(self) -> None:
        self._queue.put((99, 0, None))

    # -- tools ------------------------------------------------------------------------------------
    def probe(self, path: Path) -> Optional[Dict[str, Any]]:
        if not self.ffprobe_bin:
            return None
        try:
            done = subprocess.run(
                [self.ffprobe_bin, "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                 str(path)], stdin=subprocess.DEVNULL, capture_output=True, timeout=PROBE_TIMEOUT_S,
                umask=0o077)
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode != 0:
            return None
        try:
            data = json.loads(done.stdout.decode("utf-8", "replace") or "{}")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def ffmpeg(self, args: List[str], out: Path, *, fmt: str, timeout: Optional[float] = None) -> bool:
        """``ffmpeg -y -v error <args> -f <fmt> <out>.part`` → renamed to *out* (0600) on success."""
        if not self.ffmpeg_bin:
            return False
        part = out.with_name(out.name + ".part")
        cmd = self._prefix + [self.ffmpeg_bin, "-nostdin", "-y", "-v", "error", "-threads", THREADS,
                              *args, "-f", fmt, str(part)]
        try:
            done = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, timeout=timeout or self.timeout, umask=0o077)
            ok = done.returncode == 0 and part.is_file() and part.stat().st_size > 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if not ok:
            try:
                part.unlink()
            except OSError:
                pass
            return False
        os.chmod(part, 0o600)
        os.replace(part, out)
        return True

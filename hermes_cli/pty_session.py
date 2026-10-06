"""Keep-alive PTY sessions for dashboard terminals.

A PTY process outlives the WebSocket that created it: a single drain task always reads the PTY into
a bounded RingBuffer and forwards to the attached socket when present. Reconnecting with the same
opaque token replays the buffer and resumes live.

A PTY also knows which Hermes session it hosts (the TUI writes it to its active-session file), so a
tab that comes back WITHOUT its token — a browser restart that restored the URL but not
sessionStorage, a reload that changed the ``?resume=`` key — is handed the orphaned PTY for that
session instead of spawning a second TUI that the first one's lease then refuses (WB-781).
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, FrozenSet, Optional, Tuple

WS_CLOSE_PROCESS_EXITED = 4410
WS_CLOSE_SUPERSEDED = 4409
TUI_FORCE_REDRAW = b"\x0c"
# Browsers throttle a hidden tab's timers to about one wake-up per minute, and the /chat client
# sends a keepalive every 20 s while its socket is open. A socket silent for two minutes is a
# half-open leg — typically a reverse proxy (``tailscale serve``) still holding the loopback side of
# a browser that is gone; loopback uvicorn runs without WS pings, so nothing else ever notices. It
# counts as detached: its PTY may be adopted by the restored tab and becomes reapable.
PTY_VIEWER_STALE_S = 120.0
_HOSTED_POLL_S = 1.0


@dataclass(frozen=True)
class PtyIdentity:
    """What a /api/pty connection asks for, beyond its attach key.

    ``tab``: the browser tab's raw ``?attach=`` token. ``variant``: everything besides the session
    that makes two TUIs non-interchangeable (profile, provider, model, ...). ``resume_ids``: the
    session ids the tab asked to resume (raw and canonical). ``session_file``: where the spawned
    TUI records the session it currently hosts.
    """

    tab: str = ""
    variant: Tuple[str, ...] = ()
    resume_ids: FrozenSet[str] = field(default_factory=frozenset)
    session_file: Optional[Path] = None
    spawn_resume: Optional[str] = None


def read_hosted_session(path: Optional[Path]) -> Optional[str]:
    """The stored session id a TUI last wrote to its active-session file, or None."""
    if path is None:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sid = data.get("session_id") if isinstance(data, dict) else None
    return str(sid).strip() or None if sid else None


def session_control_frame(session_id: str) -> str:
    """Text frame telling the browser which session this PTY hosts (the tab records it in its URL)."""
    return json.dumps({"type": "session", "id": session_id})


class RingBuffer:
    """Keeps only the most recent ``capacity`` bytes appended to it."""

    def __init__(self, capacity: int) -> None:
        self._cap = capacity
        self._buf = bytearray()
        self.truncated = False

    def append(self, data: bytes) -> None:
        self._buf.extend(data)
        overflow = len(self._buf) - self._cap
        if overflow > 0:
            del self._buf[:overflow]
            self.truncated = True

    def snapshot(self) -> bytes:
        return bytes(self._buf)


async def _close_ws(ws, code: int) -> None:
    try:
        if ws is not None:
            await ws.close(code=code)
    except Exception:
        pass


class PtySession:
    def __init__(self, key: str, bridge, *, buffer_cap: int, read_timeout: float,
                 identity: Optional[PtyIdentity] = None) -> None:
        self.key = key
        self.bridge = bridge
        self.buffer = RingBuffer(buffer_cap)
        self.alive = True
        self.attached = False
        self.last_detached_at: Optional[float] = None
        # Last time the attached viewer sent anything (input or the client keepalive).
        self.last_seen_at: float = time.monotonic()
        self.last_output_at: float = self.last_seen_at
        self.identity = identity or PtyIdentity()
        self._announced: Optional[str] = None
        self._announce_ws = None
        self._hosted_checked_at = 0.0
        self._read_timeout = read_timeout
        self._ws = None
        self._attach_generation = 0
        self._drain_task: Optional[asyncio.Task] = None
        self._write_lock = asyncio.Lock()

    def hosted_session(self) -> Optional[str]:
        """The Hermes session this PTY's TUI currently shows (follows /new, /resume inside it)."""
        return read_hosted_session(self.identity.session_file) or self.identity.spawn_resume

    def touch(self, now: Optional[float] = None) -> None:
        self.last_seen_at = time.monotonic() if now is None else now

    def idle_since(self, now: float, stale: float) -> Optional[float]:
        """Since when nobody is watching this PTY, or None while a live viewer is attached.

        A viewer that has been silent for ``stale`` seconds counts as gone: a half-open proxied
        socket would otherwise pin the PTY — and its session lease — forever, and refuse the same
        user's restored tab as "another window". Such a PTY is idle from its last sign of life on
        either side, so a turn that is still printing is not reaped under a suspended phone tab.
        """
        if not self.attached:
            return self.last_detached_at
        if now - self.last_seen_at > stale:
            return max(self.last_seen_at, self.last_output_at)
        return None

    def viewer_gone(self, now: float, stale: float) -> bool:
        """No live browser tab shows this PTY (detached, or its socket went silent)."""
        return not self.attached or now - self.last_seen_at > stale

    async def _announce_hosted(self, ws, *, force: bool = False) -> None:
        # Only to a viewer that asked (``?session_frames=1``): an older client would print the
        # JSON frame into its terminal.
        if ws is None or ws is not self._announce_ws:
            return
        sid = self.hosted_session()
        if not sid or (sid == self._announced and not force):
            return
        try:
            await ws.send_text(session_control_frame(sid))
            self._announced = sid
        except Exception:
            pass

    async def start(self) -> None:
        self._drain_task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            chunk = await loop.run_in_executor(None, self.bridge.read, self._read_timeout)
            if chunk is None:                       # EOF — the agent process exited
                self.alive = False
                await _close_ws(self._ws, WS_CLOSE_PROCESS_EXITED)
                return
            now = time.monotonic()
            if self.identity.session_file is not None and now - self._hosted_checked_at >= _HOSTED_POLL_S:
                self._hosted_checked_at = now
                await self._announce_hosted(self._ws)
            if not chunk:                            # idle tick
                await asyncio.sleep(0)
                continue
            self.last_output_at = now
            self.buffer.append(chunk)
            ws = self._ws
            try:
                if ws is not None:
                    await ws.send_bytes(chunk)
            except Exception:
                # The viewer is gone; nothing else observes this failure (the handler's finally
                # only runs once ws.receive() sees the disconnect). detach() is a no-op when a
                # replacement socket attached during the send, so the new viewer keeps its session.
                self.detach(ws)

    async def write(self, ws, data: bytes) -> bool:
        """Serialize input and discard bytes from a superseded socket."""
        async with self._write_lock:
            if self._ws is not ws:
                return True
            generation = self._attach_generation
            delivered = await self.bridge.write(data)
            # A replacement socket can attach while the bridge write is
            # suspended on backpressure. A late failure from the superseded
            # socket must not poison the replacement's shared PTY session.
            if (
                not delivered
                and self._ws is ws
                and self._attach_generation == generation
            ):
                self.alive = False
            return delivered

    async def attach(self, ws, *, force_redraw: bool = False, session_frames: bool = False) -> bool:
        """Attach a browser terminal and replay buffered PTY output.

        The TUI renders differentially on an alternate screen, so a bounded ANSI tail is not a
        self-contained frame; ``force_redraw`` asks the live TUI for one full redraw after replay.
        ``session_frames``: the client understands ``{"type":"session"}`` text frames.
        """
        if self._ws is not ws:
            await _close_ws(self._ws, WS_CLOSE_SUPERSEDED)
        self._ws = ws
        self._announce_ws = ws if session_frames else None
        self._attach_generation += 1
        self.attached = True
        self.last_detached_at = None
        self.touch()
        # Every (re)attach learns which session it shows, so the tab's URL carries it into the
        # next browser restart even when the tab was opened as a fresh /chat.
        await self._announce_hosted(ws, force=True)
        if snap := self.buffer.snapshot():
            try:
                await ws.send_bytes(snap)
            except Exception:
                # Client dropped mid-replay; the caller never reaches its writer loop, so undo the
                # attach here or reap_idle() can never reclaim this PTY (#110849).
                self.detach(ws)
                return False
        if force_redraw:
            return await self.write(ws, TUI_FORCE_REDRAW)
        return True

    def detach(self, ws) -> None:
        # Only the currently-attached socket may mark the session detached: a superseded socket's
        # handler also calls detach on its way out (after the new tab attached), and flipping
        # ``attached`` then would make a session with a live viewer look idle and reapable.
        if self._ws is not ws:
            return
        self._ws = None
        self._announce_ws = None
        self.attached = False
        self.last_detached_at = time.monotonic()

    async def close(self) -> None:
        self.alive = False
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            # bridge.close() joins the child — blocking; keep it off the event loop.
            # See #53227.
            await asyncio.to_thread(self.bridge.close)
        except Exception:
            pass


class RegistryFull(Exception):
    """Every keep-alive slot holds a PTY that some tab is still attached to."""

    def __init__(self, message: str = "Too many chat terminals are open in other tabs; close one and try again.") -> None:
        super().__init__(message)


async def run_reaper(registry: "PtySessionRegistry", *, interval: float = 60.0) -> None:
    """Periodically reap idle/dead keep-alive sessions. Cancelled on shutdown."""
    while True:
        await asyncio.sleep(interval)
        try:
            await registry.reap_idle()
        except Exception:
            pass


class PtySessionRegistry:
    def __init__(self, *, ttl: float, max_sessions: int, buffer_cap: int, read_timeout: float,
                 viewer_stale: float = PTY_VIEWER_STALE_S) -> None:
        self._ttl = ttl
        self._max = max_sessions
        self._buffer_cap = buffer_cap
        self._read_timeout = read_timeout
        self._viewer_stale = viewer_stale
        self._sessions: Dict[str, PtySession] = {}
        # The get-or-spawn decision spans awaits (reap_idle, the spawn thread,
        # session.start), so two connections racing one attach token both saw
        # "no session" and forked a PTY each: the token then mapped to whichever
        # registered last while the other tab's live session fell out of the
        # registry — never reaped, and a reattach landed on the wrong terminal
        # (#115304). Serialize the decision so a token maps to one PTY.
        # ponytail: one registry-wide lock, not per key — argv resolution is
        # already serialized globally for the same reason, and a spawn only
        # delays NEW chats. Per-key locks if spawn throughput ever matters.
        self._attach_lock = asyncio.Lock()
        # Sessions popped from the registry but still closing in the background; close_all()
        # awaits them too, and holding the tasks keeps them from being garbage-collected.
        self._background_closes: set[asyncio.Task] = set()

    def _adoptable(self, identity: Optional[PtyIdentity], now: float) -> Optional[PtySession]:
        """A living PTY already hosting the session ``identity`` asks to resume, that no OTHER live
        tab is showing — the tab's own PTY from before a browser restart or a reload that lost or
        re-keyed its attach token (WB-781). Spawning a second TUI there instead would be refused by
        the first one's session lease ("open in another Hermes window") until the reaper got to it.
        A PTY another tab still shows is never taken: that one is a genuinely foreign live owner.
        """
        if identity is None or not identity.resume_ids:
            return None
        found = [
            s for s in self._sessions.values()
            if s.alive and s.identity.variant == identity.variant
            and s.hosted_session() in identity.resume_ids
            and (s.viewer_gone(now, self._viewer_stale) or (identity.tab and s.identity.tab == identity.tab))
        ]
        if not found:
            return None
        # Several orphans for one session (earlier restarts): the tab's own first, then the newest.
        return max(found, key=lambda s: (s.identity.tab == identity.tab, s.last_output_at))

    async def attach_or_spawn(self, key: str, *, spawn: Callable[[], object],
                              identity: Optional[PtyIdentity] = None) -> Tuple[PtySession, bool]:
        await self.reap_idle()
        async with self._attach_lock:
            existing = self._sessions.get(key)
            if existing is not None and existing.alive:
                return existing, False
            if existing is not None:                       # dead remnant
                # Close in the background: ending a dead leader's helpers can take the helper
                # grace, and this lock serializes every new chat.
                self._sessions.pop(key, None)
                self._close_in_background(existing)
            adopted = self._adoptable(identity, time.monotonic())
            if adopted is not None:
                self._sessions.pop(adopted.key, None)
                adopted.key = key
                if identity is not None:
                    adopted.identity = PtyIdentity(
                        tab=identity.tab, variant=identity.variant,
                        resume_ids=identity.resume_ids | adopted.identity.resume_ids,
                        session_file=adopted.identity.session_file,
                        spawn_resume=adopted.identity.spawn_resume)
                self._sessions[key] = adopted
                return adopted, False
            if len(self._sessions) >= self._max:
                self._reap_one_idle_or_raise()
            # PTY spawn does blocking fork/exec work — keep it off the event loop.
            # See #53227.
            bridge = await asyncio.to_thread(spawn)
            session = PtySession(key, bridge, buffer_cap=self._buffer_cap, read_timeout=self._read_timeout,
                                 identity=identity)
            await session.start()
            self._sessions[key] = session
            return session, True

    async def close_other_sessions(self, prefix: str, *, keep_key: str) -> None:
        """Close sessions belonging to the same logical client except ``keep_key``.

        Dashboard profile changes keep the browser's attach token but change the
        canonical session key. The previous profile's detached PTY must not
        remain alive long enough to hold the TUI session lease and reject a
        later return to that chat.
        """
        async with self._attach_lock:
            keys = [
                key for key in self._sessions
                if key != keep_key and (key == prefix or key.startswith(prefix + "\0"))
            ]
            for key in keys:
                session = self._sessions.pop(key, None)
                if session is not None:
                    # A sibling tab sharing the attach token may still be viewing this
                    # PTY: supersede it explicitly (4409) instead of leaving it silent
                    # until its next keystroke fails with 1013.
                    await _close_ws(session._ws, WS_CLOSE_SUPERSEDED)
                    await session.close()

    def detach(self, key: str, ws) -> None:
        s = self._sessions.get(key)
        if s is not None:
            s.detach(ws)

    def touch(self, key: str) -> None:
        """The viewer on ``key`` just sent something — it is alive, not a half-open leftover."""
        s = self._sessions.get(key)
        if s is not None:
            s.touch()

    def _idle_since(self, s: PtySession, now: float) -> Optional[float]:
        return s.idle_since(now, self._viewer_stale)

    async def reap_idle(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        doomed = []
        for key, s in self._sessions.items():
            idle = self._idle_since(s, now)
            # EOF never arrives if a helper still holds the PTY slave after the child died (#76759);
            # ask the process itself (a WNOHANG waitpid).
            if not s.alive or (idle is not None and (now - idle) > self._ttl) or not s.bridge.is_alive():
                doomed.append(key)
        for key in doomed:
            # Reaps overlap (attach_or_spawn and the background reaper) and close()
            # awaits, so a concurrent reap can have popped this key already — skip
            # it instead of raising KeyError into the websocket handler.
            session = self._sessions.pop(key, None)
            if session is not None:
                if session.attached:
                    # Half-open viewer: tell whatever is left of it why the terminal went away.
                    await _close_ws(session._ws, WS_CLOSE_PROCESS_EXITED)
                await session.close()

    def _reap_one_idle_or_raise(self) -> None:
        now = time.monotonic()
        idle = [(since, s) for s in self._sessions.values() if (since := self._idle_since(s, now)) is not None]
        if not idle:
            raise RegistryFull()
        _since, oldest = min(idle, key=lambda pair: pair[0])
        self._sessions.pop(oldest.key, None)
        self._close_in_background(oldest)

    def _close_in_background(self, session: "PtySession") -> None:
        task = asyncio.create_task(session.close())
        self._background_closes.add(task)
        task.add_done_callback(self._background_closes.discard)

    async def close_all(self) -> None:
        # Close concurrently: each close() may wait out its helpers' SIGHUP grace, and shutdown runs
        # under the backend's SIGTERM -> SIGKILL budget (dashboard_procs._POSIX_TERM_GRACE_SECONDS).
        sessions = [self._sessions.pop(key) for key in list(self._sessions)]
        await asyncio.gather(*(s.close() for s in sessions), *self._background_closes, return_exceptions=True)

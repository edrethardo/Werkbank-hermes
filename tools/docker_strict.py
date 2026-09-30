"""Opt-in Docker tool policy. Only foreground terminal execution is admitted.

This is a model-tool boundary, not a sandbox for the trusted Hermes controller,
plugins, image publisher or operator. Never infer permission from a toolset name.
"""
from pathlib import Path
import os
import stat

STRICT_TOOLS = frozenset({"terminal"})


def strict_enabled() -> bool:
    from tools.terminal_scope import terminal_env
    from tools.terminal_tool import _ensure_terminal_env_bridged
    _ensure_terminal_env_bridged()
    value = terminal_env("TERMINAL_DOCKER_STRICT", "false").strip().lower()
    if value not in {"true", "false", "1", "0", "yes", "no"}:
        raise ValueError("terminal.docker_strict must be a boolean")
    return value in {"true", "1", "yes"}


def tool_denial(name: str, args: dict | None = None) -> str | None:
    try:
        if not strict_enabled():
            return None
        from tools.terminal_scope import terminal_env
        if terminal_env("TERMINAL_ENV", "local") != "docker":
            return "docker_strict requires terminal.backend: docker; no host fallback is permitted."
        if name not in STRICT_TOOLS:
            return f"docker_strict denies {name}; use foreground terminal inside the container."
        args = args or {}
        if args.get("background") or args.get("pty") or args.get("_host_local"):
            return "docker_strict denies background, PTY and host-local terminal execution."
    except Exception as exc:
        return f"docker_strict policy unavailable: {exc}"
    return None


def validate_volumes(volumes) -> list[str]:
    """Canonical existing bind sources only; no named/anonymous volumes or sockets.

    Inspect metadata, never file contents. Resolve symlinks and scan directory
    descendants so binding a parent directory cannot smuggle a Unix socket in.
    Operators must keep approved bind sources trusted for the container lifetime.
    """
    if volumes is None:
        return []
    if not isinstance(volumes, list):
        raise ValueError("docker_strict volumes must be a list")
    known = [Path(p) for p in (
        "/var/run/docker.sock", "/run/docker.sock", "/run/podman/podman.sock",
        f"/run/user/{os.getuid()}/docker.sock", f"/run/user/{os.getuid()}/podman/podman.sock",
        str(Path.home() / ".docker/run/docker.sock"),
        str(Path.home() / ".docker/desktop/docker.sock"),
    )]
    known += [p.resolve() for p in known]
    result = []
    for volume in volumes:
        if not isinstance(volume, str):
            raise ValueError("docker_strict volume must be a string")
        parts = volume.split(":")
        if len(parts) not in (2, 3) or (len(parts) == 3 and parts[2] not in {"ro", "rw"}):
            raise ValueError("docker_strict requires /absolute/source:/absolute/target[:ro|rw]")
        source, target = parts[:2]
        if not source.startswith("/") or not target.startswith("/"):
            raise ValueError("docker_strict requires absolute bind paths (no named volumes)")
        src = Path(source).resolve(strict=True)
        dst = Path(target)
        if ".." in dst.parts or dst == Path("/") or any(dst == p or p in dst.parents for p in map(Path, ("/proc", "/sys", "/dev"))):
            raise ValueError("docker_strict rejects system mount targets")
        if any(src == p or src in p.parents for p in known):
            raise ValueError("docker_strict rejects Docker socket paths and their parents")
        pending, seen = [src], set()
        while pending:
            path = pending.pop().resolve(strict=True)
            if path in seen:
                continue
            seen.add(path)
            if any(path == p or p in path.parents for p in map(Path, ("/proc", "/sys", "/dev"))):
                raise ValueError("docker_strict rejects host system filesystems")
            mode = path.stat().st_mode
            if stat.S_ISDIR(mode):
                pending.extend(path.iterdir())
            elif not stat.S_ISREG(mode):
                raise ValueError("docker_strict rejects sockets and other special files in bind sources")
        result.append(f"{src}:{target}:{parts[2] if len(parts) == 3 else 'rw'}")
    return result


def validate_extra_args(args) -> list[str]:
    # Empty allowlist deliberately: dedicated config keys cover resources,
    # env, binds and network. Arbitrary Docker flags can override all guards.
    if args not in (None, []):
        raise ValueError("docker_strict rejects docker_extra_args; use dedicated configuration keys")
    return []

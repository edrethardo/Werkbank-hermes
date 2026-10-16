"""Opt-in Docker tool policy: foreground terminal and container-only text reads.

This is a model-tool boundary, not a sandbox for the trusted Hermes controller,
plugins, image publisher or operator. Never infer permission from a toolset name.
"""
from pathlib import Path, PurePosixPath
import os
import re
import stat

STRICT_TOOLS = frozenset({"terminal", "read_file"})


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


def _docker_socket_paths() -> list[Path]:
    """Known Docker/Podman API socket paths, literal and resolved."""
    known = [Path(p) for p in (
        "/var/run/docker.sock", "/run/docker.sock", "/run/podman/podman.sock",
        f"/run/user/{os.getuid()}/docker.sock", f"/run/user/{os.getuid()}/podman/podman.sock",
        str(Path.home() / ".docker/run/docker.sock"),
        str(Path.home() / ".docker/desktop/docker.sock"),
    )]
    return known + [p.resolve() for p in known]


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
    known = _docker_socket_paths()
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



BOARD_SOCKET_NAME = "board.sock"
BOARD_SOCKET_TARGET_DIR = PurePosixPath("/run/werkbank")
LABEL_PREFIX = "werkbank."
MAX_LABELS = 4
_LABEL_KEY_RE = re.compile(r"werkbank(\.[a-z0-9]+(-[a-z0-9]+)*)+")


def _private_and_owned(st: os.stat_result, what: str) -> None:
    if st.st_uid != os.getuid():
        raise ValueError(f"docker_board_socket {what} must be owned by the calling user")
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise ValueError(f"docker_board_socket {what} must be private (no group/other bits: 0600 socket, 0700 directory)")


def validate_board_socket(spec, *, require_present: bool = True) -> list[str]:
    """The ONE Unix socket strict Docker may bind: the Werkbank board door (WB-1177).

    ``docker_volumes`` keeps rejecting every socket; this is a separate, narrower door.
    ``spec`` is ``/abs/run/board.sock:/run/werkbank/<name>`` (no mode; always ``rw`` because
    connecting needs write). The source must be exactly that socket: normalized, no symlink
    anywhere in the path, named ``board.sock``, owned by the caller with no group/other bits,
    inside a caller-owned 0700 directory; never a known Docker/Podman socket (by path, parent or
    inode) and never ``$SSH_AUTH_SOCK``. ``require_present=False`` (the read-only probe) checks
    everything but the socket itself when it is not bound yet; container start always requires it.
    Returns ``[]`` (unset) or exactly one ``source:target:rw`` bind.
    """
    if spec is None or spec == "":
        return []
    if not isinstance(spec, str) or "," in spec or spec.count(":") != 1:
        raise ValueError("docker_board_socket takes exactly one /abs/board.sock:/run/werkbank/<name>")
    source, target = spec.split(":")
    dst = PurePosixPath(target)
    if (not target.startswith("/") or str(dst) != target or "." in target.split("/")
            or ".." in dst.parts or dst.parent != BOARD_SOCKET_TARGET_DIR or not dst.name):
        raise ValueError(f"docker_board_socket target must be a file directly under {BOARD_SOCKET_TARGET_DIR}/")
    if not source.startswith("/") or os.path.normpath(source) != source or "//" in source:
        raise ValueError("docker_board_socket source must be a normalized absolute path")
    src = Path(source)
    known = _docker_socket_paths()
    if any(src == p or src in p.parents for p in known):
        raise ValueError("docker_board_socket rejects Docker socket paths and their parents")
    if os.path.realpath(src.parent) != str(src.parent):
        raise ValueError("docker_board_socket rejects a symlink in the source path")
    if src.name != BOARD_SOCKET_NAME:
        raise ValueError(f"docker_board_socket source must be named {BOARD_SOCKET_NAME}")
    parent = os.lstat(src.parent)
    if not stat.S_ISDIR(parent.st_mode):
        raise ValueError("docker_board_socket source directory must be a directory")
    _private_and_owned(parent, "directory")
    try:
        st = os.lstat(src)
    except FileNotFoundError:
        if require_present:
            raise ValueError(f"docker_board_socket source {src} does not exist") from None
        return [f"{src}:{target}:rw"]
    if stat.S_ISLNK(st.st_mode):
        raise ValueError("docker_board_socket rejects a symlink in the source path")
    if not stat.S_ISSOCK(st.st_mode):
        raise ValueError("docker_board_socket source must be a Unix socket, not a directory or file")
    for path in known:
        try:
            other = os.stat(path)
        except OSError:
            continue
        if (other.st_dev, other.st_ino) == (st.st_dev, st.st_ino):
            raise ValueError("docker_board_socket rejects Docker socket paths and their parents (same inode)")
    agent = os.environ.get("SSH_AUTH_SOCK")
    if agent and os.path.realpath(agent) == str(src):
        raise ValueError("docker_board_socket rejects the SSH_AUTH_SOCK agent socket")
    _private_and_owned(st, "socket")
    return [f"{src}:{target}:rw"]


def validate_labels(labels) -> dict[str, str]:
    """Extra container labels for strict Docker: only ``werkbank.*`` keys, at most four.

    Values must already be label-safe (``[A-Za-z0-9_.-]``, 1..63 chars, the rule of
    ``_sanitize_label_value``): a lossy rewrite could make two runs share an identity, so
    anything that would be rewritten is rejected instead.
    """
    if labels is None:
        return {}
    if not isinstance(labels, dict):
        raise ValueError("docker_labels must be a mapping of werkbank.* keys to values")
    if len(labels) > MAX_LABELS:
        raise ValueError(f"docker_labels allows at most {MAX_LABELS} labels")
    from tools.environments.docker import _sanitize_label_value
    result = {}
    for key, value in labels.items():
        if not isinstance(key, str) or not _LABEL_KEY_RE.fullmatch(key):
            raise ValueError(f"docker_labels keys must start with {LABEL_PREFIX!r} (got {key!r})")
        if not isinstance(value, str) or not value or _sanitize_label_value(value) != value:
            raise ValueError(f"docker_labels label value for {key} must match [A-Za-z0-9_.-]{{1,63}}")
        result[key] = value
    return result

"""``hermes sandbox-probe``: report the EFFECTIVE strict-Docker tool policy of this profile.

A trusted launcher (the Werkbank sandbox handshake, WB-855/WB-1064) must not trust a config
file it wrote: a profile ``.env``, the ambient environment, a managed-scope overlay or
``HERMES_IGNORE_USER_CONFIG`` can change what Hermes actually runs with. This command asks
the binary itself. It resolves the terminal policy with ``apply_terminal_config_to_env`` on a
COPY of the environment -- the bridge the terminal tool's own ``_ensure_terminal_env_bridged``
runs. ``hermes chat`` projects config.yaml earlier through a DIFFERENT function,
``cli_config_load._mirror_config_to_env``; both follow the same precedence rule (a config.yaml
with a ``terminal`` section overrides ``TERMINAL_*`` env, otherwise env wins), but they are two
code paths, so equality is pinned by tests, not by construction. ``--sandbox-config`` (WB-1176)
pins one policy; the probe reads it back AND runs the start path (pin + ``import cli``) in a
child to compare the ``os.environ`` the run's subprocesses inherit (``probe_sandbox_config``). The result is
parsed with the terminal tool's own reader (``_get_env_config`` under a bound terminal scope).
It never mutates ``os.environ``, starts Docker, opens the network or touches the session store
(the ``--sandbox-config`` child imports ``cli`` with this profile, which loads config and ``.env``).

Contract (``hermes -p <profile> sandbox-probe --config <file> --json``), one JSON object:

- ``strict``: True only when ``terminal.docker_strict`` is on AND the backend is Docker.
- ``config_sha256``: sha256 of the config.yaml this profile really runs with; ``None`` when
  no such file governs the run, or when env/managed overrides make the effective terminal
  policy differ from that file plus defaults (``overrides`` names them). The Docker runtime
  selection (``HERMES_DOCKER_BINARY``, ``DOCKER_HOST``, ``DOCKER_CONTEXT``, ``DOCKER_CONFIG``)
  counts as an override when set: another daemon resolves bind paths on another host, another
  binary is other code. ``docker_binary`` is the CLI ``find_docker()`` resolves.
- ``docker_network``: False only for a Docker backend with ``docker_network: false``.
- ``auth_exposed``: False only when strict and no credential path into the container is
  configured (no forwarded env, no credential-named ``docker_env``, no extra Docker args, no
  bind of the Hermes home or a host credential directory, and a ``docker_board_socket`` only
  when it passes the container start's own socket rules). ``auth_reasons`` explains.
  ``docker_env`` is judged by variable NAME; values are only screened for a few well-known
  key shapes (``ghp_``/``github_pat_``, ``sk-``, ``AKIA``, ``xox?-``, ``scheme://user:pw@``).
  A secret in any other shape under a harmless name is NOT detected -- the launcher that
  writes ``docker_env`` owns its values. Home-relative credential paths are taken from both
  ``$HOME`` and the passwd entry, and the real default Hermes root from the passwd home.
- ``board_socket``: the one approved board-door socket (``source``/``target``/``present``) or
  ``None``; ``labels``: the ``werkbank.*`` container labels (WB-1177).

``--config`` names the run's config file; ``config_matches`` says whether its bytes equal the
effective config. Exit 0 = report printed (read the fields, not the exit code); non-zero =
nothing provable (missing file, unparseable config, unreadable policy).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

_CREDENTIAL_NAME = re.compile(
    r"(KEY|TOKEN|SECRET|PASSW|PASSPHRASE|CREDENTIAL|AUTH|COOKIE|SESSION|PRIVATE|BEARER|_PAT$)",
    re.IGNORECASE)
# Well-known secret shapes in a docker_env VALUE (the name check is primary; this is a backstop).
_CREDENTIAL_VALUE = re.compile(
    r"(?:\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}|\bsk-[A-Za-z0-9_-]{20,}"
    r"|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bxox[abposr]-[A-Za-z0-9-]{10,}"
    r"|[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@)")
# Environment that selects WHICH container runtime/daemon runs the sandbox (find_docker + docker CLI).
DOCKER_RUNTIME_ENV = ("DOCKER_CONFIG", "DOCKER_CONTEXT", "DOCKER_HOST", "HERMES_DOCKER_BINARY")
# Host paths whose bind (or a bind of an ancestor / descendant) hands credentials to the container.
_CREDENTIAL_HOME_ENTRIES = (
    ".ssh", ".aws", ".azure", ".config", ".docker", ".gnupg", ".kube", ".netrc",
    ".git-credentials", ".claude", ".claude.json", ".codex", ".npmrc", ".pypirc",
    ".local/share/keyrings", ".password-store")


class ProbeError(Exception):
    """Nothing provable: the effective policy could not be resolved."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _profile_name(home: Path) -> str:
    return home.name if home.parent.name == "profiles" else "default"


def _read_config_file(path: Path) -> Optional[Dict[str, Any]]:
    """Parsed config.yaml, ``None`` when absent; ProbeError when present but not a mapping."""
    from utils import fast_safe_load

    try:
        with open(path, encoding="utf-8-sig") as stream:
            data = fast_safe_load(stream)
    except FileNotFoundError:
        return None
    except Exception as exc:
        raise ProbeError(f"cannot read {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ProbeError(f"{path} is not a mapping")
    return data


def _effective_terminal_env(environ: Mapping[str, str], raw: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """TERMINAL_* as a run of this profile gets them (same rule as ``_ensure_terminal_env_bridged``)."""
    from hermes_cli.config import apply_terminal_config_to_env

    target = {k: v for k, v in environ.items() if k.startswith("TERMINAL_")}
    if environ.get("HERMES_IGNORE_USER_CONFIG") == "1":
        # --ignore-user-config: config.yaml is not authority -> shipped defaults under the env.
        return {**_file_only_terminal_env(None), **target}
    if raw is not None and isinstance(raw.get("terminal"), dict):
        apply_terminal_config_to_env(env=target, override=True)
    elif "TERMINAL_ENV" not in target:
        apply_terminal_config_to_env(env=target, override=False)
    return {k: v for k, v in target.items() if k.startswith("TERMINAL_")}


def _file_only_terminal_env(raw: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """What the config file plus shipped defaults alone would produce (no env, no overlay)."""
    from hermes_cli.config import apply_terminal_config_to_env
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    terminal = dict(DEFAULT_CONFIG.get("terminal") or {})
    raw_terminal = (raw or {}).get("terminal")
    if isinstance(raw_terminal, dict):
        terminal.update(raw_terminal)
    target: Dict[str, str] = {}
    apply_terminal_config_to_env(env=target, config={"terminal": terminal}, override=True)
    return target


def _overrides(effective: Dict[str, str], file_only: Dict[str, str]) -> List[str]:
    from hermes_cli.config import TERMINAL_CONFIG_ENV_MAP

    return sorted(var for var in set(TERMINAL_CONFIG_ENV_MAP.values())
                  if effective.get(var) != file_only.get(var))


def _parse_policy(effective: Dict[str, str]) -> Dict[str, Any]:
    """The terminal tool's own reading of *effective* (raises ValueError when it would refuse)."""
    from tools.docker_strict import strict_enabled
    from tools.terminal_scope import reset_terminal_scope, set_terminal_scope
    from tools.terminal_tool import _get_env_config

    token = set_terminal_scope(dict(effective))
    try:
        strict = strict_enabled()
        cfg = _get_env_config()
    finally:
        reset_terminal_scope(token)
    cfg["docker_strict"] = bool(strict and cfg.get("docker_strict"))
    return cfg


def _user_homes() -> List[Path]:
    """``$HOME`` AND the passwd home: a moved ``$HOME`` must not hide the real ``~/.ssh``."""
    homes = [Path.home()]
    try:
        import pwd
        homes.append(Path(pwd.getpwuid(os.getuid()).pw_dir))
    except (ImportError, KeyError):  # no passwd database (Windows, stripped containers)
        pass
    result: List[Path] = []
    for h in homes:
        real = Path(os.path.realpath(h))
        if real not in result:
            result.append(real)
    return result


def _sensitive_paths(home: Path) -> List[Path]:
    from hermes_constants import get_default_hermes_root

    suffix = os.environ.get("HERMES_DATA_DIR_SUFFIX", "")
    paths = [home, get_default_hermes_root()]
    for user_home in _user_homes():
        # The real default Hermes root, independent of $HOME and HERMES_HOME.
        paths += [user_home / ".hermes", user_home / (".hermes" + suffix)]
        paths += [user_home / entry for entry in _CREDENTIAL_HOME_ENTRIES]
    return list(dict.fromkeys(Path(os.path.realpath(p)) for p in paths))


def _overlaps(source: Path, sensitive: Path) -> bool:
    return source == sensitive or source in sensitive.parents or sensitive in source.parents


def _auth_reasons(cfg: Dict[str, Any], home: Path) -> List[str]:
    reasons: List[str] = []
    if not cfg.get("docker_strict"):
        reasons.append("docker_strict is off: Hermes may inject .env passthrough, skills or "
                       "egress-proxy credentials into the tool environment")
    if cfg.get("docker_forward_env"):
        reasons.append("docker_forward_env forwards host values: "
                       + ", ".join(map(str, cfg["docker_forward_env"])))
    docker_env = cfg.get("docker_env") or {}
    env_names = [str(k) for k in docker_env if _CREDENTIAL_NAME.search(str(k))]
    if env_names:
        reasons.append("docker_env sets credential-named variables: " + ", ".join(sorted(env_names)))
    if isinstance(docker_env, dict):
        shaped = [str(k) for k, v in docker_env.items()
                  if str(k) not in env_names and _CREDENTIAL_VALUE.search(str(v))]
        if shaped:  # name only: the value is the secret
            reasons.append("docker_env values look like credentials: " + ", ".join(sorted(shaped)))
    if cfg.get("docker_extra_args"):
        reasons.append("docker_extra_args can override every guard")
    user_homes = _user_homes()
    sensitive = _sensitive_paths(home)
    for volume in cfg.get("docker_volumes") or []:
        source = str(volume).split(":", 1)[0]
        if not source.startswith("/"):
            reasons.append(f"non-absolute volume source {source!r}")
            continue
        src = Path(os.path.realpath(source))
        if any(src == h or src in h.parents for h in user_homes) or any(
                _overlaps(src, p) for p in sensitive):
            reasons.append(f"volume {source} overlaps the Hermes home or a host credential path")
    reasons += _board_socket_reasons(cfg, sensitive)
    if cfg.get("docker_labels"):
        from tools.docker_strict import validate_labels
        try:
            validate_labels(cfg["docker_labels"])
        except ValueError as exc:
            reasons.append(f"docker_labels rejected: {exc}")
    return reasons


def _board_socket(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The one board-door socket strict Docker would bind (WB-1177), ``None`` when unset."""
    spec = cfg.get("docker_board_socket")
    if not spec:
        return None
    source, _, target = str(spec).partition(":")
    return {"source": source, "target": target, "present": os.path.lexists(source)}


def _board_socket_reasons(cfg: Dict[str, Any], sensitive: List[Path]) -> List[str]:
    """The board socket is an approved door only when it passes the SAME rules the container start
    applies (exact socket path, no parent directory, no symlink, caller-owned and private, never a
    Docker socket). Not yet bound is fine here: the container start re-checks the live socket."""
    spec = cfg.get("docker_board_socket")
    if not spec:
        return []
    if not cfg.get("docker_strict"):
        return ["docker_board_socket is set but docker_strict is off"]
    from tools.docker_strict import validate_board_socket
    try:
        validate_board_socket(spec, require_present=False)
    except (OSError, ValueError) as exc:
        return [f"docker_board_socket rejected: {exc}"]
    source = Path(str(spec).split(":", 1)[0])
    if any(_overlaps(source, p) for p in sensitive):
        return [f"docker_board_socket {source} lies in the Hermes home or a host credential path"]
    return []


def _docker_runtime_overrides(environ: Mapping[str, str]) -> List[str]:
    """Runtime-selection variables that are set: the config file cannot vouch for them."""
    return [var for var in DOCKER_RUNTIME_ENV if str(environ.get(var) or "").strip()]


def _docker_binary() -> Optional[str]:
    """The container CLI this process would run (``find_docker()``), ``None`` when absent."""
    try:
        from tools.environments.docker import find_docker
        return find_docker()
    except Exception:  # an unresolvable runtime is reported, not raised
        return None


def probe(requested_config: Path, *, environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Build the report for this process' HERMES_HOME (``-p`` already re-homed it)."""
    from hermes_constants import get_hermes_home

    environ = os.environ if environ is None else environ
    requested = Path(requested_config)
    try:
        requested_sha = _sha256(requested)
    except OSError as exc:
        raise ProbeError(f"cannot read --config {requested}: {exc}") from exc

    home = Path(get_hermes_home())
    config_path = home / "config.yaml"
    raw = _read_config_file(config_path)
    ignored = environ.get("HERMES_IGNORE_USER_CONFIG") == "1"

    effective = _effective_terminal_env(environ, raw)
    overrides = sorted(_overrides(effective, _file_only_terminal_env(None if ignored else raw))
                       + _docker_runtime_overrides(environ))
    strict_reason = None
    cfg: Dict[str, Any]
    try:
        cfg = _parse_policy(effective)
    except ValueError as exc:  # the terminal tool would refuse to run at all
        cfg, strict_reason = {"env_type": effective.get("TERMINAL_ENV", "local")}, str(exc)
    backend = str(cfg.get("env_type") or "local")
    strict = bool(cfg.get("docker_strict")) and backend == "docker"
    if strict_reason is None and not strict:
        strict_reason = ("terminal.docker_strict is off" if backend == "docker"
                         else f"backend is {backend!r}, not docker")
    cfg["docker_strict"] = strict
    auth_reasons = _auth_reasons(cfg, home)
    effective_sha = None
    if raw is not None and not ignored and not overrides:
        effective_sha = _sha256(config_path)
    return {
        "strict": strict,
        "config_sha256": effective_sha,
        "docker_network": not (backend == "docker" and cfg.get("docker_network") is False),
        "auth_exposed": bool(auth_reasons),
        "backend": backend,
        "profile": _profile_name(home),
        "hermes_home": str(home),
        "config_path": str(config_path),
        "requested_config": str(requested),
        "requested_config_sha256": requested_sha,
        "config_matches": effective_sha is not None and effective_sha == requested_sha,
        "overrides": overrides,
        "auth_reasons": auth_reasons,
        "board_socket": _board_socket(cfg) if backend == "docker" else None,
        "labels": _labels(cfg) if backend == "docker" else {},
        "docker_binary": _docker_binary(),
        "strict_reason": strict_reason,
    }


def _labels(cfg: Dict[str, Any]) -> Dict[str, str]:
    labels = cfg.get("docker_labels")
    return {str(k): str(v) for k, v in labels.items()} if isinstance(labels, dict) else {}


# Child for :func:`_start_path_terminal_environ`: the start path's own steps, nothing else.
_START_PATH_CHILD = (
    "import json, os, sys\n"
    "from hermes_cli import sandbox_config\n"
    "sandbox_config.apply_sandbox_config(sandbox_config.load_sandbox_config(sys.argv[1]))\n"
    "import cli\n"
    "print('WB855-ENV:' + json.dumps({k: v for k, v in os.environ.items() if k.startswith('TERMINAL_')}))\n")


def _start_path_terminal_environ(run_config: Path) -> Optional[Dict[str, str]]:
    """``TERMINAL_*`` of ``os.environ`` as the REAL start path leaves it: pin, then ``import cli``
    (``load_hermes_dotenv`` + ``load_cli_config`` with this profile). Subprocesses of the run
    inherit exactly this. Runs in a child so this process' environment stays untouched; no model,
    no Docker. ``None`` when the child fails (nothing proven)."""
    import subprocess

    repo = str(Path(__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (repo, env.get("PYTHONPATH", "")) if p)
    try:
        proc = subprocess.run([sys.executable, "-c", _START_PATH_CHILD, str(run_config)], env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [l for l in proc.stdout.splitlines() if l.startswith("WB855-ENV:")]
    if proc.returncode != 0 or not lines:
        return None
    try:
        return {str(k): str(v) for k, v in json.loads(lines[-1][len("WB855-ENV:"):]).items()}
    except (ValueError, AttributeError):
        return None


def probe_sandbox_config(run_config: Path) -> Dict[str, Any]:
    """Report for ``hermes chat --sandbox-config FILE`` (WB-1176): same loader as the start path.

    Two checks feed ``overrides``. (1) The terminal tool's accessor under the run's pin: by
    construction this equals the policy (the pin outranks scope and env), so it only guards the
    accessor itself. (2) The real check: ``os.environ`` of the start path after the pin AND
    ``import cli`` (profile config/.env bridges run there), which every subprocess of the run
    inherits, must equal the policy (:func:`_start_path_terminal_environ`; ``start_path_unverified``
    when that child fails). ``config_sha256`` is FILE's hash only when both agree.
    """
    from hermes_cli import sandbox_config
    from hermes_cli.config import TERMINAL_CONFIG_ENV_MAP
    from hermes_constants import get_hermes_home
    from tools.terminal_scope import process_terminal_pin, terminal_env

    try:
        cfg = sandbox_config.load_sandbox_config(run_config)
    except sandbox_config.SandboxConfigError as exc:
        raise ProbeError(str(exc)) from exc
    missing = object()
    names = sorted(set(TERMINAL_CONFIG_ENV_MAP.values()) | set(cfg.policy))
    with process_terminal_pin(cfg.policy):
        seen = {name: terminal_env(name, missing) for name in names}  # type: ignore[arg-type]
    drift = {name for name in names
             if (None if seen[name] is missing else seen[name]) != cfg.policy.get(name)}
    started = _start_path_terminal_environ(Path(cfg.path))
    if started is None:
        drift.add("start_path_unverified")
    else:
        drift |= {name for name in names if started.get(name) != cfg.policy.get(name)}
    overrides = sorted(drift | set(_docker_runtime_overrides(os.environ)))
    policy = sandbox_config.read_policy(cfg.policy)
    home = Path(get_hermes_home())
    auth_reasons = _auth_reasons(policy, home)
    effective_sha = None if overrides else cfg.sha256
    return {
        "mode": "sandbox-config",
        "strict": bool(policy.get("docker_strict")) and policy.get("env_type") == "docker",
        "config_sha256": effective_sha,
        "docker_network": policy.get("docker_network") is not False,
        "auth_exposed": bool(auth_reasons),
        "backend": "docker",
        "profile": _profile_name(home),
        "hermes_home": str(home),
        "config_path": cfg.path,
        "requested_config": cfg.path,
        "requested_config_sha256": cfg.sha256,
        "config_matches": effective_sha is not None,
        "overrides": overrides,
        "auth_reasons": auth_reasons,
        "board_socket": _board_socket(policy),
        "labels": _labels(policy),
        "docker_binary": _docker_binary(),
        "strict_reason": None,
    }


def cmd_sandbox_probe(args) -> int:
    as_json = bool(getattr(args, "json", False))
    try:
        if getattr(args, "sandbox_config", None):
            report = probe_sandbox_config(Path(args.sandbox_config))
        else:
            report = probe(Path(args.config))
    except ProbeError as exc:
        if as_json:
            print(json.dumps({"error": str(exc)}))
        print(f"sandbox-probe: {exc}", file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps(report, sort_keys=True))
        return 0
    yes = {True: "yes", False: "no"}
    print(f"profile:          {report['profile']} ({report['hermes_home']})")
    print(f"strict:           {yes[report['strict']]}"
          + (f"  ({report['strict_reason']})" if report["strict_reason"] else ""))
    print(f"backend:          {report['backend']}")
    print(f"docker_network:   {yes[report['docker_network']]}")
    print(f"auth_exposed:     {yes[report['auth_exposed']]}")
    for reason in report["auth_reasons"]:
        print(f"  - {reason}")
    board = report["board_socket"]
    if board:
        print(f"board_socket:     {board['source']} -> {board['target']}"
              + ("" if board["present"] else "  (not bound yet)"))
    for key, value in sorted(report["labels"].items()):
        print(f"label:            {key}={value}")
    print(f"docker_binary:    {report['docker_binary'] or '-'}")
    print(f"config_sha256:    {report['config_sha256'] or '-'}")
    print(f"config_matches:   {yes[report['config_matches']]}")
    if report["overrides"]:
        print("overrides:        " + ", ".join(report["overrides"]))
    return 0

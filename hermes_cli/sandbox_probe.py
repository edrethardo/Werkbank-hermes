"""``hermes sandbox-probe``: report the EFFECTIVE strict-Docker tool policy of this profile.

A trusted launcher (the Werkbank sandbox handshake, WB-855/WB-1064) must not trust a config
file it wrote: a profile ``.env``, the ambient environment, a managed-scope overlay or
``HERMES_IGNORE_USER_CONFIG`` can change what Hermes actually runs with. This command asks
the binary itself. It resolves the terminal policy through the SAME config->env bridge a run
of this profile uses (``apply_terminal_config_to_env`` on a COPY of the environment) and
parses it with the terminal tool's own reader (``_get_env_config`` under a bound terminal
scope). It never mutates ``os.environ``, starts Docker, opens the network or touches the
session store.

Contract (``hermes -p <profile> sandbox-probe --config <file> --json``), one JSON object:

- ``strict``: True only when ``terminal.docker_strict`` is on AND the backend is Docker.
- ``config_sha256``: sha256 of the config.yaml this profile really runs with; ``None`` when
  no such file governs the run, or when env/managed overrides make the effective terminal
  policy differ from that file plus defaults (``overrides`` names them).
- ``docker_network``: False only for a Docker backend with ``docker_network: false``.
- ``auth_exposed``: False only when strict and no credential path into the container is
  configured (no forwarded env, no credential-named ``docker_env``, no extra Docker args, no
  bind of the Hermes home or a host credential directory). ``auth_reasons`` explains.

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


def _sensitive_paths(home: Path) -> List[Path]:
    from hermes_constants import get_default_hermes_root

    user_home = Path.home()
    paths = [home, get_default_hermes_root()]
    paths += [user_home / entry for entry in _CREDENTIAL_HOME_ENTRIES]
    return [Path(os.path.realpath(p)) for p in paths]


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
    env_names = [str(k) for k in (cfg.get("docker_env") or {}) if _CREDENTIAL_NAME.search(str(k))]
    if env_names:
        reasons.append("docker_env sets credential-named variables: " + ", ".join(sorted(env_names)))
    if cfg.get("docker_extra_args"):
        reasons.append("docker_extra_args can override every guard")
    user_home = Path(os.path.realpath(Path.home()))
    sensitive = _sensitive_paths(home)
    for volume in cfg.get("docker_volumes") or []:
        source = str(volume).split(":", 1)[0]
        if not source.startswith("/"):
            reasons.append(f"non-absolute volume source {source!r}")
            continue
        src = Path(os.path.realpath(source))
        if src == user_home or src in user_home.parents or any(_overlaps(src, p) for p in sensitive):
            reasons.append(f"volume {source} overlaps the Hermes home or a host credential path")
    return reasons


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
    overrides = _overrides(effective, _file_only_terminal_env(None if ignored else raw))
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
        "strict_reason": strict_reason,
    }


def cmd_sandbox_probe(args) -> int:
    as_json = bool(getattr(args, "json", False))
    try:
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
    print(f"config_sha256:    {report['config_sha256'] or '-'}")
    print(f"config_matches:   {yes[report['config_matches']]}")
    if report["overrides"]:
        print("overrides:        " + ", ".join(report["overrides"]))
    return 0

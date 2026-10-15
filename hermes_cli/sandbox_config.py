"""Run config for a sandboxed worker: ``hermes chat --sandbox-config FILE`` (WB-1176).

A trusted launcher (Werkbank WB-855) writes a per-run ``config.yaml`` that describes strict Docker
and proves it with ``hermes sandbox-probe --sandbox-config FILE`` before it starts the worker
with ``hermes chat --sandbox-config FILE``. Both paths go through :func:`load_sandbox_config`, so
the hash the probe reports is the hash of the bytes the run applies.

What the file may contain: ONE top-level key, ``terminal``, whose keys are known ``terminal.*``
settings. It must say ``backend: docker`` and ``docker_strict: true`` and pass the strict checks
the Docker backend itself applies (volumes, extra args). Anything else is refused: an ignored key
would make the hash claim more than the run applies.

What the start path does: the file's terminal block plus shipped defaults becomes the COMPLETE
``TERMINAL_*`` policy of the process (``tools.terminal_scope.pin_process_terminal_policy``). The
profile's ``terminal.*``, its ``.env`` and the ambient ``TERMINAL_*`` environment no longer reach
the terminal tool. Model access, memory, skills and the session store stay the profile's (host).
"""

from __future__ import annotations

import hashlib
import io
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


class SandboxConfigError(Exception):
    """The file does not describe a strict-Docker run this process can apply."""


@dataclass(frozen=True)
class SandboxConfig:
    path: str
    sha256: str
    terminal: Dict[str, Any] = field(repr=False)
    policy: Dict[str, str] = field(repr=False)  # complete TERMINAL_* mapping the run uses


_applied: Optional[SandboxConfig] = None


def applied_sandbox_config() -> Optional[SandboxConfig]:
    """The run config this process started with, ``None`` without ``--sandbox-config``."""
    return _applied


def _parse(path: Path) -> tuple[bytes, Dict[str, Any]]:
    from utils import fast_safe_load

    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SandboxConfigError(f"cannot read {path}: {exc}") from exc
    try:
        # Parse exactly the hashed bytes (no second read: what is hashed is what is applied).
        loaded = fast_safe_load(io.StringIO(data.decode("utf-8-sig")))
    except Exception as exc:
        raise SandboxConfigError(f"{path} is not valid YAML/JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise SandboxConfigError(f"{path} must be a mapping with a 'terminal' section")
    return data, loaded


def _validated_terminal(raw: Dict[str, Any], path: Path) -> Dict[str, Any]:
    from hermes_cli.config import TERMINAL_CONFIG_ENV_MAP

    extra = sorted(str(k) for k in raw if k != "terminal")
    if extra:
        raise SandboxConfigError(f"{path} may only contain 'terminal' (found: {', '.join(extra)})")
    terminal = raw.get("terminal")
    if not isinstance(terminal, dict) or not terminal:
        raise SandboxConfigError(f"{path} has no 'terminal' section")
    unknown = sorted(str(k) for k in terminal if k not in TERMINAL_CONFIG_ENV_MAP)
    if unknown:
        raise SandboxConfigError(f"{path} has unknown terminal keys: {', '.join(unknown)}")
    if terminal.get("backend") != "docker":
        raise SandboxConfigError(f"{path} must set terminal.backend: docker")
    if terminal.get("docker_strict") is not True:
        raise SandboxConfigError(f"{path} must set terminal.docker_strict: true")
    return terminal


def sandbox_terminal_policy(terminal: Dict[str, Any]) -> Dict[str, str]:
    """Shipped terminal defaults <- the file's terminal block, as a complete ``TERMINAL_*`` map.

    The profile is deliberately NOT a layer: whatever the file leaves out takes the shipped
    default, so the file plus this Hermes version fully determine the policy.
    """
    from hermes_cli.config import TERMINAL_CONFIG_ENV_MAP, _terminal_env_value
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    merged = {**(DEFAULT_CONFIG.get("terminal") or {}), **terminal}
    policy: Dict[str, str] = {}
    for key, env_var in TERMINAL_CONFIG_ENV_MAP.items():
        value = merged.get(key)
        if value is None or (key == "cwd" and str(value).strip() in {".", "auto", "cwd"}):
            continue
        policy[env_var] = _terminal_env_value(value)
    policy["TERMINAL_DOCKER_STRICT"] = "true"
    policy["TERMINAL_DOCKER_IMAGE_PINNED"] = "1" if "docker_image" in terminal else "0"
    return policy


def read_policy(policy: Dict[str, str]) -> Dict[str, Any]:
    """The terminal tool's own reading of *policy* (raises ValueError where it would refuse)."""
    from tools.docker_strict import strict_enabled, validate_extra_args, validate_volumes
    from tools.terminal_scope import process_terminal_pin
    from tools.terminal_tool import _get_env_config

    with process_terminal_pin(policy):
        strict = strict_enabled()
        cfg = _get_env_config()
    if not (strict and cfg.get("docker_strict") and cfg.get("env_type") == "docker"):
        raise ValueError("the terminal tool does not read this as strict Docker")
    validate_extra_args(cfg.get("docker_extra_args"))
    validate_volumes(cfg.get("docker_volumes"))
    return cfg


def load_sandbox_config(path: os.PathLike | str) -> SandboxConfig:
    """Load, validate and project a run config. Shared by ``chat`` and ``sandbox-probe``."""
    path = Path(path)
    data, raw = _parse(path)
    terminal = _validated_terminal(raw, path)
    policy = sandbox_terminal_policy(terminal)
    try:
        read_policy(policy)
    except ValueError as exc:
        raise SandboxConfigError(f"{path} is not a valid strict-Docker config: {exc}") from exc
    return SandboxConfig(path=str(path), sha256=hashlib.sha256(data).hexdigest(),
                         terminal=terminal, policy=policy)


def apply_sandbox_config(cfg: SandboxConfig) -> None:
    """Pin *cfg* as this process' terminal policy for the rest of its life."""
    global _applied
    from hermes_cli.config import TERMINAL_CONFIG_ENV_MAP
    from tools.terminal_scope import pin_process_terminal_policy

    pin_process_terminal_policy(cfg.policy)
    _applied = cfg
    # Mirror into os.environ for the few ambient readers (status lines, file-path hints). The
    # pin, not this mirror, is the authority: later env writes cannot reach the terminal tool.
    owned = set(TERMINAL_CONFIG_ENV_MAP.values()) | set(cfg.policy)
    for name in [n for n in os.environ if n.startswith("TERMINAL_") and n in owned]:
        del os.environ[name]
    os.environ.update(cfg.policy)


def _reset_for_tests() -> None:
    global _applied
    from tools.terminal_scope import _clear_process_terminal_pin

    _applied = None
    _clear_process_terminal_pin()

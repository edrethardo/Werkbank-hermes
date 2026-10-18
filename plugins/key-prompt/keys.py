"""Which providers may be asked for, which profiles may receive, and the actual store."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def allowed_providers() -> Dict[str, str]:
    """``{provider_id: display name}``: the API-key rows of ``hermes_cli.auth.PROVIDER_REGISTRY``
    plus OpenRouter (API-key first, kept outside the registry). OAuth/device-code providers are
    excluded on purpose — a pasted string cannot complete their login."""
    from hermes_cli.auth import PROVIDER_REGISTRY
    allowed = {pid: cfg.name for pid, cfg in PROVIDER_REGISTRY.items()
               if getattr(cfg, "auth_type", "") == "api_key"}
    allowed.setdefault("openrouter", "OpenRouter")
    return allowed


def normalize_provider(raw) -> Optional[str]:
    pid = str(raw or "").strip().lower()
    return pid if pid in allowed_providers() else None


def profile_names() -> List[str]:
    from hermes_cli.profiles import list_profile_names
    try:
        return list_profile_names()
    except Exception:
        return ["default"]


def profile_home(name: str) -> Path:
    from hermes_cli.profiles import get_profile_dir
    return Path(get_profile_dir(name))


@contextmanager
def _home(path: Path):
    # Writing into another profile's auth.json from the dashboard (multiplex mode) also
    # needs that profile's secret scope, or the credential pool raises UnscopedSecretError.
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(path)
    scope = set_secret_scope(build_profile_secret_scope(path), profile_home=str(path))
    try:
        yield
    finally:
        reset_secret_scope(scope)
        reset_hermes_home_override(token)


def register_redaction(value: str) -> None:
    """Exact-value scrub of model-facing output for the CURRENT profile scope."""
    try:
        from agent.redact import register_vault_redaction_value
        register_vault_redaction_value(value)
    except Exception:
        pass


def save_api_key(provider: str, value: str, *, label: str, profile: str) -> Tuple[str, str]:
    """Store via ``add_api_key_credential`` (= ``hermes auth add --type api-key``) into
    ``profile``'s credential pool. Returns ``(entry_id, label)``; raises ``ValueError``."""
    if profile not in profile_names():
        raise ValueError("unknown profile")
    from hermes_cli.auth_commands import add_api_key_credential
    register_redaction(value)
    with _home(profile_home(profile)):
        entry = add_api_key_credential(provider, value, label=label or None)
        register_redaction(value)
    return entry.id, entry.label


def register_stored_value(provider: str, profile: str, entry_id: str) -> None:
    """Agent side: after the page stored a key, scrub its exact value from this profile's
    model-facing output too. Reads the pool; the value is never returned."""
    try:
        from agent.credential_pool import load_pool
        with _home(profile_home(profile)):
            entries = load_pool(provider).entries()
        for entry in entries:
            if entry.id == entry_id and entry.access_token:
                register_redaction(entry.access_token)
    except Exception:
        pass

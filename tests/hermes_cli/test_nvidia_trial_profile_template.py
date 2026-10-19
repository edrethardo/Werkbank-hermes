"""The ``nvidia-trial`` profile template stays inside the WB-1216 rules (offline)."""

from __future__ import annotations

from pathlib import Path

import pytest
import hermes_yaml as yaml

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "docs" / "notes" / "nvidia-trial-profile.config.yaml"


@pytest.fixture(scope="module")
def overlay():
    return yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def confirmed():
    import model_tools  # noqa: F401 — registers provider plugins
    import providers
    return set(providers.get_provider_profile("nvidia").fallback_models)


def test_main_model_is_a_confirmed_nvidia_id(overlay, confirmed):
    assert overlay["model"]["provider"] == "nvidia"
    assert overlay["model"]["default"] in confirmed


def test_fallback_chain_leaves_nvidia(overlay):
    from hermes_cli.fallback_config import get_fallback_chain
    chain = get_fallback_chain(overlay)
    assert [e["provider"] for e in chain] == ["anthropic", "openai-codex"]
    assert all(e.get("model") for e in chain)


def test_delegation_profiles_use_confirmed_ids_and_declare_a_fallback(overlay, confirmed):
    from hermes_cli.fallback_config import get_fallback_chain
    from tools.delegate_tool_config import _get_model_profiles
    profiles = _get_model_profiles(overlay["delegation"])
    assert set(profiles) == {"nim-general", "nim-ultra", "nim-vision"}
    for name, route in profiles.items():
        assert route["provider"] == "nvidia", name
        assert route["model"] in confirmed, name
        chain = get_fallback_chain({"fallback_providers": route["fallback_providers"]})
        assert chain and all(e["provider"] != "nvidia" for e in chain), name


def test_no_unconfirmed_deepseek(overlay):
    assert "deepseek" not in TEMPLATE.read_text(encoding="utf-8").split("absent.")[1]


def test_personal_data_sources_are_off(overlay):
    assert overlay["memory"] == {"memory_enabled": False, "user_profile_enabled": False}


def test_side_tasks_stay_off_nvidia(overlay, monkeypatch):
    import agent.aux_provider_policy as policy
    monkeypatch.setattr(policy, "_load_config", lambda: overlay)
    assert policy.main_route_excluded("nvidia", "https://integrate.api.nvidia.com/v1")


def test_no_secrets_and_no_invented_context(overlay):
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "api_key:" not in text and "nvapi-" not in text
    assert "context_length:" not in text


SANDBOX = "~/.hermes/profiles/nvidia-trial/sandbox"
DOC = REPO / "docs" / "notes" / "nvidia-trial-profile.md"


def test_terminal_cwd_is_a_real_key_on_a_throwaway_dir(overlay):
    """Tool output (ls, file contents) goes to NVIDIA: the terminal must start in a sandbox, not $HOME."""
    from hermes_cli.config import apply_terminal_config_to_env
    assert overlay["terminal"]["cwd"] == SANDBOX
    env = apply_terminal_config_to_env(env={}, config=overlay, override=True)
    expanded = Path(env["TERMINAL_CWD"])
    assert expanded.is_absolute() and expanded != Path.home()
    assert expanded.parts[-3:] == ("profiles", "nvidia-trial", "sandbox")


def test_plugins_and_mcp_servers_are_off(overlay):
    assert overlay["plugins"] == {"enabled": []}  # explicit empty allow-list, not "unset"
    assert overlay["mcp_servers"] == {}


def test_doc_creates_the_profile_fresh_not_as_a_clone():
    text = DOC.read_text(encoding="utf-8")
    assert "hermes profile create nvidia-trial --no-skills" in text
    assert "--clone-from" not in text.split("## Profil anlegen")[1].split("```")[1]
    assert SANDBOX in text

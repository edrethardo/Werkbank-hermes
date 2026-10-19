"""Side tasks stay off a trial main provider (WB-1216).

NVIDIA Build is a trial endpoint (no personal data, dev load only). With
``provider: nvidia`` as the session's main model, the ``auto`` auxiliary route
used to send every side task — the title prompt, compression summaries, vision,
the background memory/skill review — to NVIDIA as well. Policy now:

* ``auxiliary.exclude_main_providers`` (default ``["nvidia"]`` when unset) names
  main providers that ``auto`` side tasks never route back onto;
* those tasks use an explicit ``auxiliary.<task>`` route, else the configured
  fallback chain (minus excluded providers), else they are OFF — never a guessed
  other provider;
* ``exclude_main_providers: []`` restores the old main-model-first behaviour;
* a local NIM (``NVIDIA_BASE_URL`` on another host) is not the trial endpoint.

No HTTP: every client is a fake.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import agent.aux_provider_policy as policy
import agent.auxiliary_client as aux

NIM_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_RUNTIME = {"provider": "nvidia", "model": "nvidia/nemotron-3-super-120b-a12b", "base_url": NIM_URL,
                  "api_key": "fake-key-not-real", "api_mode": "chat_completions"}


def _cfg(aux_block=None, **top):
    return {"auxiliary": dict(aux_block or {}), **top}


@pytest.fixture
def config(monkeypatch):
    """Mutable config seen by the policy and the main fallback chain reader."""
    box = {"cfg": _cfg()}
    monkeypatch.setattr(policy, "_load_config", lambda: box["cfg"])
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: box["cfg"])
    return box


# ── the policy itself ──────────────────────────────────────────────────────────

class TestPolicy:
    def test_default_excludes_hosted_nvidia_and_its_aliases(self, config):
        assert policy.main_route_excluded("nvidia", NIM_URL)
        assert policy.main_route_excluded("nvidia", "")  # stock base_url = hosted
        assert policy.main_route_excluded("nim", "")
        assert policy.main_route_excluded("NVIDIA-NIM", "")

    def test_custom_route_to_the_hosted_endpoint_is_excluded_too(self, config):
        assert policy.main_route_excluded("custom", "https://integrate.api.nvidia.com/v1/")

    def test_local_nim_is_not_the_trial(self, config):
        assert not policy.main_route_excluded("nvidia", "http://localhost:8000/v1")

    def test_other_providers_are_untouched(self, config):
        for provider in ("anthropic", "openai-codex", "openrouter", "custom", "auto", ""):
            assert not policy.main_route_excluded(provider, "")

    def test_empty_list_switches_the_policy_off(self, config):
        config["cfg"] = _cfg({"exclude_main_providers": []})
        assert not policy.main_route_excluded("nvidia", NIM_URL)

    def test_list_is_configurable(self, config):
        config["cfg"] = _cfg({"exclude_main_providers": ["openrouter"]})
        assert policy.main_route_excluded("openrouter", "")
        assert not policy.main_route_excluded("nvidia", NIM_URL)

    def test_malformed_value_keeps_the_safe_default(self, config):
        config["cfg"] = _cfg({"exclude_main_providers": 42})
        assert policy.main_route_excluded("nvidia", NIM_URL)

    def test_unreadable_config_keeps_the_safe_default(self, monkeypatch):
        def boom():
            raise OSError("no config")
        monkeypatch.setattr(policy, "_load_config", boom)
        assert policy.main_route_excluded("nvidia", NIM_URL)


# ── text side tasks (title, compression, memory query rewrite, …) ─────────────

class TestAutoRoute:
    def test_title_never_reaches_nvidia_and_uses_the_fallback_chain(self, config):
        config["cfg"] = _cfg(fallback_providers=[
            {"provider": "nvidia", "model": "nvidia/nemotron-3-ultra-550b-a55b"},
            {"provider": "anthropic", "model": "claude-sonnet-5-5"},
        ])
        claude = MagicMock(name="claude")
        calls = []

        def fake_resolve(provider, model=None, **_kw):
            calls.append(provider)
            return (claude, model) if provider == "anthropic" else (MagicMock(name=provider), model)

        with patch.object(aux, "resolve_provider_client", side_effect=fake_resolve), \
                patch.object(aux, "_resolve_fallback_entry",
                             side_effect=lambda e: fake_resolve(e["provider"], e["model"])), \
                patch.object(aux, "_is_provider_unhealthy", return_value=False), \
                patch.object(aux, "_try_openrouter") as openrouter:
            client, model, provider = aux._resolve_auto_route(main_runtime=NVIDIA_RUNTIME, task="title_generation")

        assert client is claude and model == "claude-sonnet-5-5" and "anthropic" in provider
        assert "nvidia" not in calls
        openrouter.assert_not_called()

    def test_without_a_fallback_the_task_is_off_not_guessed(self, config, caplog):
        with patch.object(aux, "resolve_provider_client") as resolve, \
                patch.object(aux, "_try_openrouter") as openrouter, \
                patch.object(aux, "_try_nous") as nous, \
                caplog.at_level(logging.INFO, logger=policy.logger.name):
            client, model, provider = aux._resolve_auto_route(main_runtime=NVIDIA_RUNTIME, task="compression")
        assert (client, model, provider) == (None, None, "")
        resolve.assert_not_called()
        openrouter.assert_not_called()
        nous.assert_not_called()
        assert "exclude_main_providers" in caplog.text

    def test_switch_off_restores_main_model_first(self, config):
        config["cfg"] = _cfg({"exclude_main_providers": []})
        nim = MagicMock(name="nim")
        with patch.object(aux, "resolve_provider_client", return_value=(nim, NVIDIA_RUNTIME["model"])) as resolve, \
                patch.object(aux, "_is_provider_unhealthy", return_value=False):
            client, _model, _provider = aux._resolve_auto_route(main_runtime=NVIDIA_RUNTIME, task="title_generation")
        assert client is nim
        assert resolve.call_args.args[0] == "nvidia"

    def test_other_main_providers_keep_main_model_first(self, config):
        claude = MagicMock(name="claude")
        with patch.object(aux, "resolve_provider_client", return_value=(claude, "claude-sonnet-5-5")) as resolve, \
                patch.object(aux, "_is_provider_unhealthy", return_value=False):
            client, _m, _p = aux._resolve_auto_route(
                main_runtime={"provider": "anthropic", "model": "claude-sonnet-5-5"}, task="title_generation")
        assert client is claude and resolve.call_args.args[0] == "anthropic"

    def test_main_fallback_chain_skips_nvidia_for_other_main_providers(self, config):
        """Claude main, NVIDIA as first backup: a side task after a Claude failure skips NVIDIA."""
        config["cfg"] = _cfg(fallback_providers=[
            {"provider": "nvidia", "model": "nvidia/nemotron-3-super-120b-a12b"},
            {"provider": "openai-codex", "model": "gpt-5.6-terra"},
        ])
        codex = MagicMock(name="codex")
        resolved = []

        def fake_entry(entry):
            resolved.append(entry["provider"])
            return (codex if entry["provider"] == "openai-codex" else MagicMock()), entry["model"]

        with patch.object(aux, "_resolve_fallback_entry", side_effect=fake_entry), \
                patch.object(aux, "_is_provider_unhealthy", return_value=False):
            client, model, _label = aux._try_main_fallback_chain("title_generation", "anthropic")
        assert client is codex and model == "gpt-5.6-terra"
        assert resolved == ["openai-codex"]

    def test_last_resort_main_model_fallback_skips_nvidia(self, config):
        with patch.object(aux, "_read_main_provider", return_value="nvidia"), \
                patch.object(aux, "_read_main_model", return_value=NVIDIA_RUNTIME["model"]), \
                patch.object(aux, "resolve_provider_client") as resolve:
            assert aux._try_main_agent_model_fallback("anthropic", task="compression") == (None, None, "")
        resolve.assert_not_called()

    def test_explicit_task_route_to_nvidia_is_respected(self, config, monkeypatch):
        """``auxiliary.<task>.provider: nvidia`` is an explicit choice, not ``auto`` (client only, no HTTP)."""
        monkeypatch.setenv("NVIDIA_API_KEY", "fake-key-not-real")
        client, model = aux.resolve_provider_client("nvidia", "nvidia/nemotron-3-super-120b-a12b")
        assert client is not None
        assert "integrate.api.nvidia.com" in str(client.base_url)
        assert model == "nvidia/nemotron-3-super-120b-a12b"


def test_title_generation_sends_nothing_to_nvidia(config):
    """The WB-1215 symptom end to end: the title prompt must not become a second NVIDIA call."""
    built, main_routes = [], []
    real_main_route = aux._try_main_provider_route

    def record_main_route(*args, **kwargs):
        main_routes.append(args[0])
        return real_main_route(*args, **kwargs)

    from agent.title_generator import generate_title
    with patch.object(aux, "_try_main_provider_route", side_effect=record_main_route), \
            patch.object(aux, "_create_openai_client", side_effect=lambda **kw: built.append(kw.get("base_url"))):
        title = generate_title("Testfrage ohne Personendaten", main_runtime=dict(NVIDIA_RUNTIME))
    assert title is None
    assert main_routes == []
    assert not any("nvidia" in str(url) for url in built)


# ── vision ────────────────────────────────────────────────────────────────────

def test_vision_auto_skips_an_excluded_main_provider(config):
    with patch.object(aux, "_vision_main_provider_client") as main_step, \
            patch.object(aux, "_resolve_strict_vision_backend", return_value=(None, None)):
        assert aux._vision_auto_route(dict(NVIDIA_RUNTIME), None, None, False) == (None, None, None)
    main_step.assert_not_called()


# ── background memory/skill review ───────────────────────────────────────────

def _parent(provider="nvidia", base_url=NIM_URL):
    return SimpleNamespace(
        provider=provider, model="nvidia/nemotron-3-super-120b-a12b", client=object(), session_id="s1",
        platform="cli", request_overrides={}, max_tokens=None, acp_command=None, acp_args=[],
        enabled_toolsets=None, disabled_toolsets=None, reasoning_config=None, _credential_pool=None,
        base_url=base_url,
        _current_main_runtime=lambda: {"api_key": "fake", "base_url": base_url, "api_mode": "chat_completions"},
        _emit_auxiliary_failure=lambda *_a, **_k: None, _safe_print=lambda *_a, **_k: None,
        background_review_callback=None,
    )


def _run_review(agent, task_cfg=None, explicit=False):
    import agent.background_review as bg
    with patch("hermes_cli.config.load_config", return_value={}), \
            patch("run_agent.AIAgent") as fork, \
            patch("tools.terminal_tool.set_approval_callback"):
        bg._run_review_in_thread(agent, [{"role": "user", "content": "hi"}], "review please", task_cfg,
                                 explicit=explicit)
    return fork


def test_background_review_is_off_on_an_excluded_main_provider(config):
    assert not _run_review(_parent()).called


def test_background_review_runs_when_routed_elsewhere(config):
    import agent.background_review as bg
    routed = {"provider": "anthropic", "model": "claude-sonnet-5-5", "api_key": "k", "base_url": None,
              "api_mode": "anthropic_messages", "credential_pool": None, "request_overrides": {},
              "max_tokens": None, "command": None, "args": [], "routed": True}
    with patch.object(bg, "_resolve_review_runtime", return_value=routed):
        assert _run_review(_parent()).called


def test_explicit_refine_still_runs_on_the_main_model(config):
    """/refine is a user action in this session, not a background side task."""
    assert _run_review(_parent(), explicit=True).called


def test_background_review_unchanged_for_other_providers(config):
    assert _run_review(_parent(provider="anthropic", base_url="https://api.anthropic.com")).called

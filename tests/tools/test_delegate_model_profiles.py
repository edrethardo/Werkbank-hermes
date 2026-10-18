"""WB-1211: per-task subagent model via ``delegation.models`` + ``tasks[].model_profile``.

``delegation.models`` maps a profile name to a route ``{model, provider?, reasoning_effort?,
base_url?, api_key?, api_mode?, fallback_providers?, request_overrides?}``. A task names one with
``model_profile``; the profile is resolved through the same credential path as
``delegation.provider`` (``_resolve_delegation_credentials``) and owns the child's fallback policy
exactly like a pinned delegation route. No model is ever called: AIAgent and the runtime-provider
resolver are fakes.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.delegate_tool import (
    DELEGATE_TASK_SCHEMA,
    _build_dynamic_schema_overrides,
    delegate_task,
)
from tests.tools.test_delegate import _make_mock_parent

PARENT_CHAIN = [{"provider": "openrouter", "model": "gpt-4o-mini", "api_key": "sk-or-parent"}]
PROFILE_CHAIN = [{"provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-ds-child"}]

MODELS = {
    "schnell": {"model": "claude-sonnet-fake", "provider": "anthropic", "reasoning_effort": "low"},
    "stark": {},
}

FAKE_ANTHROPIC_RUNTIME = {
    "provider": "anthropic", "base_url": "https://api.anthropic.com", "api_key": "sk-ant-fake",
    "api_mode": "anthropic_messages",
}


def _parent():
    parent = _make_mock_parent(depth=0)
    parent._fallback_chain = list(PARENT_CHAIN)
    parent.reasoning_config = {"effort": "high"}
    parent.request_overrides = {}
    parent.requested_provider = "openrouter"
    parent.capabilities = None
    parent.acp_command = None
    parent.acp_args = []
    parent._client_kwargs = {}
    parent.client = None
    parent._credential_pool = None
    return parent


def _child_factory(built):
    def _make(**kwargs):
        child = MagicMock()
        child.model = kwargs.get("model")
        child.provider = kwargs.get("provider")
        child.session_prompt_tokens = 0
        child.session_completion_tokens = 0
        child.session_estimated_cost_usd = 0.0
        child.run_conversation.return_value = {
            "final_response": "done", "completed": True, "interrupted": False, "api_calls": 1, "messages": [],
        }
        built.append(kwargs)
        return child
    return _make


def _run(cfg, tasks, *, runtime=None):
    """Synchronous delegate_task with a fake AIAgent; returns (result_json, AIAgent kwargs list, resolver mock)."""
    built = []
    resolver = MagicMock(side_effect=lambda requested=None, target_model=None, **_: dict(runtime or FAKE_ANTHROPIC_RUNTIME))
    with patch("tools.delegate_tool._load_config", return_value=cfg), \
            patch("run_agent.AIAgent", side_effect=_child_factory(built)), \
            patch("hermes_cli.runtime_provider.resolve_runtime_provider", resolver):
        out = json.loads(delegate_task(tasks=tasks, parent_agent=_parent()))
    return out, built, resolver


def _task_props(overrides):
    return overrides["parameters"]["properties"]["tasks"]["items"]["properties"]


# ── Schema ──────────────────────────────────────────────────────────────────


def test_schema_hides_model_profile_without_configured_models():
    for cfg in ({}, {"models": {}}, {"models": None}, {"models": "not-a-map"}):
        with patch("tools.delegate_tool._load_config", return_value=cfg):
            assert "model_profile" not in _task_props(_build_dynamic_schema_overrides()), cfg
    # The static schema never advertises it either.
    assert "model_profile" not in DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"]["properties"]


def test_schema_offers_configured_profile_names_as_enum():
    with patch("tools.delegate_tool._load_config", return_value={"models": MODELS}):
        props = _task_props(_build_dynamic_schema_overrides())
    assert props["model_profile"]["type"] == "string"
    assert props["model_profile"]["enum"] == ["schnell", "stark"]
    assert "claude-sonnet-fake" in props["model_profile"]["description"]
    # Static schema dict stays untouched by the override build.
    assert "model_profile" not in DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"]["properties"]


# ── Unknown / unconfigured name → error, no child built ─────────────────────


def test_unknown_profile_is_an_error_not_a_silent_fallback():
    out, built, _ = _run({"models": MODELS}, [{"goal": "review the diff carefully", "model_profile": "turbo"}])
    assert "error" in out and "turbo" in out["error"]
    assert "schnell" in out["error"] and "stark" in out["error"]
    assert built == []


def test_profile_without_configured_models_is_an_error():
    out, built, _ = _run({}, [{"goal": "review the diff carefully", "model_profile": "schnell"}])
    assert "error" in out and "delegation.models is not configured" in out["error"]
    assert built == []


def test_non_string_profile_is_an_error():
    out, built, _ = _run({"models": MODELS}, [{"goal": "review the diff carefully", "model_profile": 3}])
    assert "error" in out
    assert built == []


def test_unresolvable_profile_provider_fails_before_any_spawn():
    def boom(**_):
        raise RuntimeError("no key for anthropic")
    built = []
    with patch("tools.delegate_tool._load_config", return_value={"models": MODELS}), \
            patch("run_agent.AIAgent", side_effect=_child_factory(built)), \
            patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=boom):
        out = json.loads(delegate_task(
            tasks=[{"goal": "measure the thing", "model_profile": "stark"},
                   {"goal": "review the diff carefully", "model_profile": "schnell"}],
            parent_agent=_parent()))
    assert "error" in out and "schnell" in out["error"]
    assert built == []


# ── Resolution ──────────────────────────────────────────────────────────────


def test_profile_resolves_through_delegation_provider_path():
    cfg = {"models": MODELS}
    out, built, resolver = _run(cfg, [
        {"goal": "review the diff carefully", "model_profile": "schnell"},
        {"goal": "build the feature fully"},
    ])
    assert "error" not in out, out
    resolver.assert_called_with(requested="anthropic", target_model="claude-sonnet-fake")
    pinned, plain = built
    assert pinned["model"] == "claude-sonnet-fake"
    assert pinned["provider"] == "anthropic"
    assert pinned["base_url"] == "https://api.anthropic.com"
    assert pinned["api_key"] == "sk-ant-fake"
    assert pinned["api_mode"] == "anthropic_messages"
    assert pinned["reasoning_config"] == {"enabled": True, "effort": "low"}
    # The task without a profile keeps today's route: the parent's.
    assert plain["model"] == "anthropic/claude-sonnet-4"
    assert plain["provider"] == "openrouter"
    assert plain["reasoning_config"] == {"effort": "high"}
    # Result entries carry the model the child actually ran.
    by_index = {r["task_index"]: r for r in out["results"]}
    assert by_index[0]["model"] == "claude-sonnet-fake"
    assert by_index[1]["model"] == "anthropic/claude-sonnet-4"


def test_empty_profile_runs_on_parent_model_even_with_global_pin():
    cfg = {"model": "global-pinned-model", "models": MODELS}
    out, built, _ = _run(cfg, [{"goal": "measure the thing", "model_profile": "stark"}])
    assert "error" not in out, out
    assert built[0]["model"] == "anthropic/claude-sonnet-4"
    assert built[0]["provider"] == "openrouter"
    # Unpinned → inherits the parent chain like any unpinned child.
    assert built[0]["fallback_model"] == PARENT_CHAIN


def test_model_only_profile_keeps_parent_provider():
    cfg = {"models": {"klein": {"model": "openai/gpt-mini-fake"}}}
    out, built, resolver = _run(cfg, [{"goal": "measure the thing", "model_profile": "klein"}])
    assert "error" not in out, out
    assert built[0]["model"] == "openai/gpt-mini-fake"
    assert built[0]["provider"] == "openrouter"
    resolver.assert_not_called()


def test_pinned_profile_does_not_borrow_parent_fallback_chain():
    out, built, _ = _run({"models": MODELS}, [{"goal": "review the diff carefully", "model_profile": "schnell"}])
    assert built[0]["fallback_model"] is None


def test_pinned_profile_uses_its_own_declared_fallback_chain():
    models = {"schnell": {**MODELS["schnell"], "fallback_providers": PROFILE_CHAIN}}
    cfg = {"models": models, "fallback_providers": [{"provider": "zai", "model": "glm-x"}]}
    out, built, _ = _run(cfg, [{"goal": "review the diff carefully", "model_profile": "schnell"}])
    assert built[0]["fallback_model"] == PROFILE_CHAIN


def test_profile_reasoning_effort_absent_falls_back_to_delegation_effort():
    cfg = {"reasoning_effort": "medium", "models": {"klein": {"model": "openai/gpt-mini-fake"}}}
    out, built, _ = _run(cfg, [{"goal": "measure the thing", "model_profile": "klein"}])
    assert built[0]["reasoning_config"] == {"enabled": True, "effort": "medium"}


def test_profile_direct_endpoint_branch():
    cfg = {"models": {"lokal": {"model": "qwen-fake", "base_url": "http://127.0.0.1:9/v1", "api_key": "sk-local"}}}
    out, built, _ = _run(cfg, [{"goal": "measure the thing", "model_profile": "lokal"}])
    assert "error" not in out, out
    assert built[0]["base_url"] == "http://127.0.0.1:9/v1"
    assert built[0]["provider"] == "custom"
    assert built[0]["api_key"] == "sk-local"
    assert built[0]["model"] == "qwen-fake"


# ── Backward compatibility ──────────────────────────────────────────────────


def _comparable(kwargs):
    return {k: v for k, v in kwargs.items() if not callable(v) and k not in {"session_db"}}


def test_missing_and_empty_models_build_identical_children():
    base_cfg = {"model": "google/gemini-fake", "reasoning_effort": "low"}
    tasks = [{"goal": "measure the thing"}]
    _, without, _ = _run(dict(base_cfg), tasks)
    _, empty, _ = _run({**base_cfg, "models": {}}, tasks)
    _, other, _ = _run({**base_cfg, "models": MODELS}, tasks)
    assert _comparable(without[0]) == _comparable(empty[0]) == _comparable(other[0])
    assert "ephemeral_system_prompt" in _comparable(without[0])


# ── Live transcript names the actual model ──────────────────────────────────


def test_live_transcript_names_the_profile_model(tmp_path):
    built = []
    seen = {}
    real_build = None
    import tools.delegate_tool as dt
    real_build = dt._build_children

    def spy(*a, **kw):
        children, err = real_build(*a, **kw)
        seen["paths"] = [getattr(c, "_live_transcript_path", None) for (_, _, c) in children]
        return children, err

    with patch("tools.delegate_tool._load_config", return_value={"models": MODELS}), \
            patch("run_agent.AIAgent", side_effect=_child_factory(built)), \
            patch("tools.delegate_tool._build_children", side_effect=spy), \
            patch("tools.delegate_tool._parent_live_home", return_value=tmp_path), \
            patch("hermes_cli.runtime_provider.resolve_runtime_provider",
                  side_effect=lambda **_: dict(FAKE_ANTHROPIC_RUNTIME)):
        out = json.loads(delegate_task(
            tasks=[{"goal": "review the diff carefully", "model_profile": "schnell"},
                   {"goal": "build the feature fully"}],
            parent_agent=_parent()))
    assert "error" not in out, out
    first, second = (Path(p).read_text(encoding="utf-8") for p in seen["paths"])
    assert "claude-sonnet-fake" in first and "anthropic" in first and "schnell" in first
    # Tasks without a profile keep today's transcript content (no extra line).
    assert "model_profile" not in second and "claude-sonnet-fake" not in second


# ── action=list / async completion metadata show the actual model ────────────


def test_running_registry_lists_the_profile_model():
    """action=list reads the live registry; its model is the one the child was built with."""
    from tools.delegate_tool import list_active_subagents
    seen = []
    built = []
    make = _child_factory(built)

    def factory(**kwargs):
        child = make(**kwargs)
        child.run_conversation.side_effect = lambda *a, **k: (
            seen.extend(r.get("model") for r in list_active_subagents()),
            {"final_response": "done", "completed": True, "interrupted": False, "api_calls": 1, "messages": []},
        )[1]
        return child

    with patch("tools.delegate_tool._load_config", return_value={"models": MODELS}), \
            patch("run_agent.AIAgent", side_effect=factory), \
            patch("hermes_cli.runtime_provider.resolve_runtime_provider",
                  side_effect=lambda **_: dict(FAKE_ANTHROPIC_RUNTIME)):
        out = json.loads(delegate_task(
            tasks=[{"goal": "review the diff carefully", "model_profile": "schnell"}], parent_agent=_parent()))
    assert "error" not in out, out
    assert "claude-sonnet-fake" in seen


def test_async_completion_model_label_follows_profile_children():
    from types import SimpleNamespace
    from tools.delegate_tool_dispatch import _unit_model_label
    unit = SimpleNamespace(creds={"model": None})
    plain = MagicMock(model="anthropic/claude-sonnet-4")
    assert _unit_model_label(unit, [plain]) is None  # today's behaviour: call-wide route
    pinned = MagicMock(model="claude-sonnet-fake")
    pinned._delegate_model_profile = "schnell"
    assert _unit_model_label(unit, [pinned, plain]) == "claude-sonnet-fake, anthropic/claude-sonnet-4"


def test_model_facing_handler_keeps_model_profile():
    from tools.delegate_tool import _strip_model_hidden_task_fields
    tasks = [{"goal": "review the diff carefully", "model_profile": "schnell", "acp_command": "x"}]
    assert _strip_model_hidden_task_fields(tasks) == [{"goal": "review the diff carefully", "model_profile": "schnell"}]

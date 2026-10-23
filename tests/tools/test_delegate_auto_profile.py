"""WB-1260: ``model_profile: "auto"`` — subagent model by difficulty, NVIDIA trial preferred.

``delegation.auto`` maps a difficulty (``leicht``/``mittel``/``schwer``) to a ``delegation.models``
profile; ``bevorzugt`` names trial profiles tried first for the stages in ``fuer``. A task gets the
trial only when it is not ``vertraulich``, the trial is healthy (credential pool, last 429/5xx,
cooldown), side tasks of the trial child stay off the trial (WB-1216 policy) and a parallel slot is
free. A trial run failing with auth/429/5xx/timeout before its first tool call restarts once on the
stage's own profile. No model is ever called: AIAgent and the runtime resolver are fakes.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import tools.delegation_auto as auto_mod
from tools.delegate_tool import _build_dynamic_schema_overrides, delegate_task
from tests.tools.test_delegate_model_profiles import _parent

NVIDIA_URL = "https://integrate.api.nvidia.com/v1"
ULTRA = "nvidia/nemotron-3-ultra-550b-a55b"

MODELS = {
    "nemotron": {"model": ULTRA, "provider": "nvidia"},
    "haiku": {"model": "claude-haiku-fake", "provider": "anthropic"},
    "schnell": {"model": "claude-sonnet-fake", "provider": "anthropic"},
    "stark": {},
}
AUTO = {
    "leicht": "haiku", "mittel": "schnell", "schwer": "stark",
    "bevorzugt": ["nemotron"], "fuer": ["leicht", "mittel"],
}

RUNTIMES = {
    "nvidia": {"provider": "nvidia", "base_url": NVIDIA_URL, "api_key": "nvapi-fake", "api_mode": "chat_completions"},
    "anthropic": {"provider": "anthropic", "base_url": "https://api.anthropic.com", "api_key": "sk-ant-fake",
                  "api_mode": "anthropic_messages"},
}

OK_RESULT = {"final_response": "done", "completed": True, "interrupted": False, "api_calls": 1, "messages": []}
RATE_LIMITED = {
    "final_response": "HTTP 429", "completed": False, "interrupted": False, "api_calls": 1, "messages": [],
    "failed": True, "error": "HTTP 429: Too Many Requests", "failure_reason": "rate_limit",
}


@pytest.fixture(autouse=True)
def _fresh_state():
    auto_mod.reset_state()
    with patch.object(auto_mod, "pool_refusal", return_value=None):
        yield
    auto_mod.reset_state()


def _factory(built, results_by_provider=None):
    """Fake AIAgent: records kwargs; ``results_by_provider[provider]`` is a list of run results (popped)."""
    results_by_provider = results_by_provider or {}

    def _make(**kwargs):
        child = MagicMock()
        child.model = kwargs.get("model")
        child.provider = kwargs.get("provider")
        child.base_url = kwargs.get("base_url")
        child.session_prompt_tokens = 0
        child.session_completion_tokens = 0
        child.session_estimated_cost_usd = 0.0
        queue = results_by_provider.get(kwargs.get("provider"))
        child.run_conversation.return_value = dict(queue.pop(0) if queue else OK_RESULT)
        built.append(kwargs)
        return child
    return _make


def _run(cfg, tasks, *, results_by_provider=None, parent=None):
    built = []
    resolver = MagicMock(side_effect=lambda requested=None, target_model=None, **_: dict(RUNTIMES[requested]))
    with patch("tools.delegate_tool._load_config", return_value=cfg), \
            patch("run_agent.AIAgent", side_effect=_factory(built, results_by_provider)), \
            patch("hermes_cli.runtime_provider.resolve_runtime_provider", resolver):
        out = json.loads(delegate_task(tasks=tasks, parent_agent=parent or _parent()))
    return out, built


def _cfg(**auto_over):
    return {"models": MODELS, "auto": {**AUTO, **auto_over}}


def _no_trial(**auto_over):
    return {"models": MODELS, "auto": {**{k: v for k, v in AUTO.items() if k not in {"bevorzugt", "fuer"}}, **auto_over}}


# ── Stufe → Profil ──────────────────────────────────────────────────────────


def test_explicit_difficulty_maps_to_configured_profile():
    out, built = _run(_no_trial(), [
        {"goal": "rename the variable foo to bar", "model_profile": "auto", "schwierigkeit": "leicht"},
        {"goal": "implement the parser change", "model_profile": "auto", "schwierigkeit": "mittel"},
        {"goal": "rename the variable baz to qux", "model_profile": "auto", "schwierigkeit": "schwer"},
    ])
    assert "error" not in out, out
    assert [b["model"] for b in built] == ["claude-haiku-fake", "claude-sonnet-fake", "anthropic/claude-sonnet-4"]
    res = {r["task_index"]: r for r in out["results"]}
    assert [res[i]["schwierigkeit"] for i in range(3)] == ["leicht", "mittel", "schwer"]
    assert [res[i]["model_profile"] for i in range(3)] == ["haiku", "schnell", "stark"]


def test_invalid_difficulty_is_an_error_before_spawn():
    out, built = _run(_no_trial(), [{"goal": "do the thing please", "model_profile": "auto", "schwierigkeit": "extrem"}])
    assert "error" in out and "schwierigkeit" in out["error"]
    assert built == []


def test_auto_mapping_to_unknown_profile_is_an_error_before_spawn():
    out, built = _run({"models": MODELS, "auto": {"leicht": "turbo", "mittel": "schnell", "schwer": "stark"}},
                      [{"goal": "do the thing please", "model_profile": "auto", "schwierigkeit": "leicht"}])
    assert "error" in out and "turbo" in out["error"]
    assert built == []


def test_auto_without_delegation_auto_is_unknown_profile_like_today():
    out, built = _run({"models": MODELS}, [{"goal": "do the thing please", "model_profile": "auto"}])
    assert "error" in out and "unknown model_profile 'auto'" in out["error"]
    assert built == []


# ── Heuristik ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("goal,context,stufe", [
    ("Review the security of the login flow", "", "schwer"),
    ("Architektur-Entscheidung für den Cache treffen", "", "schwer"),
    ("Audit the dependency licenses", None, "schwer"),
    ("List the files in docs/notes", "", "leicht"),
    ("Fasse die README zusammen", "", "leicht"),
    ("Fix it", "", "leicht"),
    ("Implement the retry option in the HTTP client and add tests for the new code path", "", "mittel"),
    ("Implement the retry option", "x" * 7000, "schwer"),
    ("Summarize this log", "y" * 2000, "mittel"),
])
def test_heuristic_is_deterministic(goal, context, stufe):
    first = auto_mod.classify(goal, context)
    assert first[0] == stufe, first
    assert auto_mod.classify(goal, context) == first  # same input, same answer, no model call
    assert first[1]  # reason is reported


def test_heuristic_keyword_needs_word_boundary():
    # "preview" must not match "review"
    assert auto_mod.classify("Render the preview image for the landing page tile and wire it into the gallery component", "")[0] == "mittel"


def test_auto_without_difficulty_uses_heuristic():
    out, built = _run(_no_trial(), [{"goal": "Review the security of the login flow", "model_profile": "auto"}])
    assert "error" not in out, out
    assert built[0]["model"] == "anthropic/claude-sonnet-4"  # schwer → stark ({} = parent model)
    r = out["results"][0]
    assert r["schwierigkeit"] == "schwer" and "review" in r["auto_grund"]


# ── Trial-Vorrang, vertraulich, Gesundheit ──────────────────────────────────


def test_trial_preferred_for_light_and_medium_not_for_hard():
    out, built = _run(_cfg(), [
        {"goal": "List the files in docs/notes", "model_profile": "auto"},
        {"goal": "Review the security of the login flow", "model_profile": "auto"},
    ])
    assert "error" not in out, out
    assert built[0]["model"] == ULTRA and built[0]["provider"] == "nvidia"
    assert built[1]["model"] == "anthropic/claude-sonnet-4"
    res = {r["task_index"]: r for r in out["results"]}
    assert res[0]["model_profile"] == "nemotron" and res[0]["provider"] == "nvidia"


def test_confidential_task_never_goes_to_trial():
    out, built = _run(_cfg(), [{"goal": "List the files in docs/notes", "model_profile": "auto", "vertraulich": True}])
    assert "error" not in out, out
    assert built[0]["model"] == "claude-haiku-fake" and built[0]["provider"] == "anthropic"
    assert "vertraulich" in out["results"][0]["auto_hinweis"]


def test_confidential_task_with_explicit_trial_profile_is_refused():
    out, built = _run(_cfg(), [{"goal": "List the files in docs/notes", "model_profile": "nemotron",
                                "vertraulich": True}])
    assert "error" in out and "vertraulich" in out["error"]
    assert built == []


def test_confidential_task_inheriting_a_trial_parent_is_refused():
    parent = _parent()
    parent.provider, parent.base_url, parent.model = "nvidia", NVIDIA_URL, ULTRA
    out, built = _run(_cfg(), [{"goal": "List the files in docs/notes", "vertraulich": True}], parent=parent)
    assert "error" in out and "vertraulich" in out["error"]
    assert built == []


def test_cooldown_after_trial_failure_then_trial_again():
    clock = [1000.0]
    with patch.object(auto_mod, "_now", side_effect=lambda: clock[0]):
        auto_mod.record_trial_failure("nemotron", "rate_limit")
        out, built = _run(_cfg(cooldown_minuten=15), [{"goal": "List the files in docs/notes", "model_profile": "auto"}])
        assert built[0]["provider"] == "anthropic"
        assert "cooldown" in out["results"][0]["auto_hinweis"].lower()
        clock[0] += 15 * 60 + 1
        out, built = _run(_cfg(cooldown_minuten=15), [{"goal": "List the files in docs/notes", "model_profile": "auto"}])
        assert built[0]["provider"] == "nvidia"


def test_unhealthy_credential_pool_skips_trial():
    with patch.object(auto_mod, "pool_refusal", return_value="Credential-Pool nvidia: kein Eintrag verfügbar"):
        out, built = _run(_cfg(), [{"goal": "List the files in docs/notes", "model_profile": "auto"}])
    assert built[0]["provider"] == "anthropic"
    assert "Credential-Pool" in out["results"][0]["auto_hinweis"]


def test_trial_refused_when_side_tasks_would_follow_it():
    """Children of the trial must not put side tasks (compression, title, review) on the trial (WB-1216)."""
    with patch("agent.aux_provider_policy.excluded_main_providers", return_value=frozenset()):
        out, built = _run(_cfg(), [{"goal": "List the files in docs/notes", "model_profile": "auto"}])
    assert built[0]["provider"] == "anthropic"
    assert "exclude_main_providers" in out["results"][0]["auto_hinweis"]


# ── Pool-Status-Lesen (nur Status-API, kein Schlüssel) ──────────────────────


def test_pool_refusal_reads_only_status_fields():
    entry = SimpleNamespace(last_status="exhausted", last_error_code=429, last_status_at=990.0)
    pool = MagicMock()
    pool.has_credentials.return_value = True
    pool.has_available.return_value = True
    pool.entries.return_value = [entry]
    with patch("agent.credential_pool.load_pool", return_value=pool), \
            patch.object(auto_mod, "_now", return_value=1000.0):
        reason = auto_mod._pool_refusal_impl("nvidia", ULTRA, 900)
    assert reason and "429" in reason
    pool.has_available.return_value = False
    with patch("agent.credential_pool.load_pool", return_value=pool), \
            patch.object(auto_mod, "_now", return_value=5000.0):
        assert "verfügbar" in auto_mod._pool_refusal_impl("nvidia", ULTRA, 900)
    pool.has_credentials.return_value = False
    with patch("agent.credential_pool.load_pool", return_value=pool):
        assert "kein" in auto_mod._pool_refusal_impl("nvidia", ULTRA, 900)


# ── Rückfall ────────────────────────────────────────────────────────────────


def test_trial_failure_before_first_tool_call_restarts_once_on_stage_profile():
    out, built = _run(_cfg(), [{"goal": "List the files in docs/notes", "model_profile": "auto"}],
                      results_by_provider={"nvidia": [RATE_LIMITED]})
    assert "error" not in out, out
    assert [b["provider"] for b in built] == ["nvidia", "anthropic"]
    r = out["results"][0]
    assert r["status"] == "completed"
    assert r["model"] == "claude-haiku-fake" and r["provider"] == "anthropic"
    assert r["fallback_von"] == "nemotron" and r["model_profile"] == "haiku"
    # The failure starts the trial cooldown for later tasks.
    assert auto_mod.trial_cooldown_reason("nemotron", 900)


def test_fallback_runs_at_most_once():
    out, built = _run(_cfg(), [{"goal": "List the files in docs/notes", "model_profile": "auto"}],
                      results_by_provider={"nvidia": [RATE_LIMITED], "anthropic": [dict(RATE_LIMITED)]})
    assert [b["provider"] for b in built] == ["nvidia", "anthropic"]
    assert out["results"][0]["status"] == "failed" and out["results"][0]["fallback_von"] == "nemotron"


def test_no_fallback_after_a_tool_call():
    with_tool = dict(RATE_LIMITED, messages=[
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ])
    out, built = _run(_cfg(), [{"goal": "List the files in docs/notes", "model_profile": "auto"}],
                      results_by_provider={"nvidia": [with_tool]})
    assert [b["provider"] for b in built] == ["nvidia"]
    assert out["results"][0]["status"] == "failed" and "fallback_von" not in out["results"][0]


def test_no_fallback_for_non_transient_error():
    bad_request = dict(RATE_LIMITED, error="HTTP 400: bad request", failure_reason="format_error")
    out, built = _run(_cfg(), [{"goal": "List the files in docs/notes", "model_profile": "auto"}],
                      results_by_provider={"nvidia": [bad_request]})
    assert [b["provider"] for b in built] == ["nvidia"]


@pytest.mark.parametrize("entry,expected", [
    ({"status": "failed", "failure_reason": "auth", "tool_trace": []}, True),
    ({"status": "failed", "failure_reason": "server_error", "tool_trace": []}, True),
    ({"status": "failed", "failure_reason": "overloaded", "tool_trace": []}, True),
    ({"status": "failed", "error": "Error code: 503 - upstream", "tool_trace": []}, True),
    ({"status": "timeout", "api_calls": 0, "timeout_phase": "before_first_llm_call"}, True),
    ({"status": "timeout", "api_calls": 2, "timeout_phase": "after_llm_calls"}, False),
    ({"status": "error", "error": "Request timed out.", "api_calls": 0}, True),
    ({"status": "failed", "failure_reason": "rate_limit", "tool_trace": [{"tool": "terminal"}]}, False),
    ({"status": "completed", "tool_trace": []}, False),
    ({"status": "failed", "failure_reason": "context_overflow", "tool_trace": []}, False),
])
def test_fallback_trigger_classification(entry, expected):
    assert bool(auto_mod.fallback_reason(entry)) is expected


# ── Parallelgrenze ──────────────────────────────────────────────────────────


def test_trial_parallel_limit_routes_overflow_to_anthropic():
    tasks = [{"goal": f"List the files in docs/notes/part{i}", "model_profile": "auto"} for i in range(3)]
    out, built = _run(_cfg(trial_parallel=2), tasks)
    assert "error" not in out, out
    assert sorted(b["provider"] for b in built) == ["anthropic", "nvidia", "nvidia"]
    overflow = [r for r in out["results"] if r["provider"] == "anthropic"]
    assert "trial_parallel" in overflow[0]["auto_hinweis"]
    # Slots are released when the children finish: the next call gets the trial again.
    out, built = _run(_cfg(trial_parallel=2), tasks[:2])
    assert [b["provider"] for b in built] == ["nvidia", "nvidia"]


def test_trial_parallel_default_is_two():
    tasks = [{"goal": f"List the files in docs/notes/part{i}", "model_profile": "auto"} for i in range(4)]
    _, built = _run(_cfg(), tasks)
    assert sorted(b["provider"] for b in built).count("nvidia") == 2


def test_slots_released_when_call_fails_after_planning():
    tasks = [{"goal": "List the files in docs/notes", "model_profile": "auto"}]
    with patch("tools.delegate_tool._oneshot_spawn_budget", return_value="budget exhausted"):
        out, _ = _run(_cfg(trial_parallel=1), tasks)
    assert out.get("error") == "budget exhausted"
    assert auto_mod.running_trial_children("nemotron") == 0


# ── Transparenz ─────────────────────────────────────────────────────────────


def test_every_result_names_model_and_provider_under_auto():
    out, _ = _run(_cfg(), [
        {"goal": "List the files in docs/notes", "model_profile": "auto"},
        {"goal": "Implement the retry option in the HTTP client and add tests", "model_profile": "schnell"},
        {"goal": "Implement the timeout option in the HTTP client and add tests"},
    ])
    res = {r["task_index"]: r for r in out["results"]}
    assert (res[0]["model"], res[0]["provider"]) == (ULTRA, "nvidia")
    assert (res[1]["model"], res[1]["provider"]) == ("claude-sonnet-fake", "anthropic")
    assert (res[2]["model"], res[2]["provider"]) == ("anthropic/claude-sonnet-4", "openrouter")


def test_without_auto_result_entries_keep_their_shape():
    out, _ = _run({"models": MODELS}, [{"goal": "Implement the retry option", "model_profile": "schnell"}])
    for key in ("provider", "schwierigkeit", "fallback_von", "auto_grund", "auto_hinweis", "model_profile"):
        assert key not in out["results"][0]


def test_async_completion_label_names_model_and_provider_under_auto():
    from tools.delegate_tool_dispatch import _unit_model_label
    unit = SimpleNamespace(creds={"model": None})
    a = MagicMock(model=ULTRA, provider="nvidia")
    a._delegate_transparent = True
    b = MagicMock(model="claude-haiku-fake", provider="anthropic")
    b._delegate_transparent = True
    assert _unit_model_label(unit, [a, b]) == f"{ULTRA} via nvidia, claude-haiku-fake via anthropic"


def test_batch_completion_block_shows_model_per_task():
    from tools.process_registry_notifications import _format_batch_delegation
    evt = {"goals": ["g0"], "results": [{"task_index": 0, "status": "completed", "summary": "ok",
                                         "model": "claude-haiku-fake", "provider": "anthropic",
                                         "fallback_von": "nemotron"}], "model": None}
    text = _format_batch_delegation(evt, "d1", 0.0)
    assert "Model: claude-haiku-fake via anthropic (fallback_von: nemotron)" in text
    plain = _format_batch_delegation({"goals": ["g0"], "results": [
        {"task_index": 0, "status": "completed", "summary": "ok", "model": "m"}]}, "d1", 0.0)
    assert "via" not in plain


# ── Schema ──────────────────────────────────────────────────────────────────


def _props():
    return _build_dynamic_schema_overrides()["parameters"]["properties"]["tasks"]["items"]["properties"]


def test_schema_offers_auto_difficulty_and_confidential_only_with_auto_config():
    with patch("tools.delegate_tool._load_config", return_value=_cfg()):
        props = _props()
    assert "auto" in props["model_profile"]["enum"]
    assert props["schwierigkeit"]["enum"] == ["leicht", "mittel", "schwer"]
    assert props["vertraulich"]["type"] == "boolean"
    with patch("tools.delegate_tool._load_config", return_value={"models": MODELS}):
        props = _props()
    assert "auto" not in props["model_profile"]["enum"]
    assert "schwierigkeit" not in props and "vertraulich" not in props


def test_standard_auto_applies_to_tasks_without_profile():
    out, built = _run(_cfg(standard=True), [{"goal": "List the files in docs/notes"}])
    assert built[0]["provider"] == "nvidia"
    out, built = _run(_cfg(), [{"goal": "List the files in docs/notes"}])
    assert built[0]["provider"] == "openrouter"  # not opted in: parent route as today

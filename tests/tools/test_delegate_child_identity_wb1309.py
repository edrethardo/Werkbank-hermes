"""WB-1309: every relayed subagent event names the model AND provider the child was built with.

The relay is made before the child's runtime is resolved; a ``model_profile``, a delegation
provider or the automatic NVIDIA route pick model/provider afterwards. The Werkbank chat shows what
the relay carries, so it must be the built child's values — not the parent's.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from tools.delegate_tool import _build_child_agent


def _parent(seen):
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "***"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.providers_allowed = parent.providers_ignored = parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent._delegate_spinner = None
    parent.thinking_callback = None
    parent.tool_progress_callback = (
        lambda event, tool_name=None, preview=None, args=None, **kw: seen.append((event, kw)))
    return parent


def _build(seen, child_model, child_provider, model_arg=None):
    parent = _parent(seen)
    with patch("run_agent.AIAgent") as MockAgent:
        child = MagicMock()
        child.model = child_model
        child.provider = child_provider
        child.session_id = "child-sess"
        MockAgent.return_value = child
        _build_child_agent(task_index=0, goal="prüfe die Tests", context=None, toolsets=None,
                           model=model_arg, max_iterations=5, parent_agent=parent, task_count=1)
        _, kwargs = MockAgent.call_args
    return kwargs["tool_progress_callback"]


def test_start_names_the_built_childs_model_and_provider():
    seen = []
    relay = _build(seen, "nemotron-3-super", "nvidia")
    relay("subagent.start", preview="prüfe die Tests")
    event, kw = seen[-1]
    assert event == "subagent.start"
    assert kw["model"] == "nemotron-3-super"
    assert kw["provider"] == "nvidia"


def test_complete_names_them_too():
    seen = []
    relay = _build(seen, "claude-haiku-4-5", "anthropic", model_arg="claude-haiku-4-5")
    relay("subagent.complete", preview="fertig", status="completed")
    event, kw = seen[-1]
    assert event == "subagent.complete"
    assert (kw["model"], kw["provider"]) == ("claude-haiku-4-5", "anthropic")


# ── Review follow-up: fallback during the run, and the real profile route ──────────────────────

def _delegate(cfg, tasks, run_conversation=None):
    """Synchronous delegate_task, only the AIAgent constructor stubbed; returns (events, built kwargs)."""
    import json
    from tests.tools.test_delegate_model_profiles import _parent as _profile_parent
    from tools.delegate_tool import delegate_task
    events, built = [], []
    parent = _profile_parent()
    parent.tool_progress_callback = (
        lambda event, tool_name=None, preview=None, args=None, **kw: events.append((event, kw)))

    def make(**kwargs):
        child = MagicMock()
        child.model, child.provider = kwargs.get("model"), kwargs.get("provider")
        child.tool_progress_callback = kwargs.get("tool_progress_callback")  # what _run_single_child reads back
        child.session_prompt_tokens = child.session_completion_tokens = 0
        child.session_estimated_cost_usd = 0.0

        def run(*a, **k):
            if run_conversation is not None:
                run_conversation(child)
            return {"final_response": "done", "completed": True, "interrupted": False, "api_calls": 1,
                    "messages": []}
        child.run_conversation.side_effect = run
        built.append(kwargs)
        return child
    with patch("tools.delegate_tool._load_config", return_value=cfg), \
            patch("run_agent.AIAgent", side_effect=make):
        out = json.loads(delegate_task(tasks=tasks, parent_agent=parent))
    assert "error" not in out, out
    assert events, "relay delivered nothing"
    return events, built


def test_complete_names_the_fallback_model_the_child_ended_on():
    def fall_back(child):
        child.model, child.provider = "fallback-model", "fallback-prov"
    cfg = {"models": {"lokal": {"model": "qwen-fake", "base_url": "http://127.0.0.1:9/v1", "api_key": "sk-l"}}}
    events, _ = _delegate(cfg, [{"goal": "measure the thing", "model_profile": "lokal"}], fall_back)
    start = next(kw for e, kw in events if e == "subagent.start")
    done = next(kw for e, kw in events if e == "subagent.complete")
    assert (start["model"], start["provider"]) == ("qwen-fake", "custom")
    assert (done["model"], done["provider"]) == ("fallback-model", "fallback-prov")


def test_failed_child_complete_names_the_current_model_too():
    def boom(child):
        child.model = "fallback-model"
        raise RuntimeError("kaputt")
    cfg = {"models": {"lokal": {"model": "qwen-fake", "base_url": "http://127.0.0.1:9/v1", "api_key": "sk-l"}}}
    events, _ = _delegate(cfg, [{"goal": "measure the thing", "model_profile": "lokal"}], boom)
    done = next(kw for e, kw in events if e == "subagent.complete")
    assert done["model"] == "fallback-model"


def test_real_runtime_resolution_with_model_profile_reaches_the_relay():
    """Nothing but the constructor is stubbed: the real ``_resolve_child_runtime`` runs with a
    ``model_profile`` (direct endpoint, so no credential lookup) and the relay names the result."""
    cfg = {"models": {"lokal": {"model": "qwen-fake", "base_url": "http://127.0.0.1:9/v1", "api_key": "sk-l"}}}
    events, built = _delegate(cfg, [{"goal": "measure the thing", "model_profile": "lokal"}])
    assert built[0]["model"] == "qwen-fake" and built[0]["base_url"] == "http://127.0.0.1:9/v1"
    for event in ("subagent.start", "subagent.complete"):
        kw = next(kw for e, kw in events if e == event)
        assert (kw["model"], kw["provider"]) == ("qwen-fake", "custom"), event

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

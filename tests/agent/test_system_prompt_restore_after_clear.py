"""WB-1426: after a gateway ``/clear`` the session's history is empty but its system prompt is
stored — the next turn must reuse those bytes (prompt cache, project/cwd text), not rebuild."""

from __future__ import annotations

from unittest.mock import MagicMock

from agent.conversation_loop import _restore_or_build_system_prompt

STORED = "SYSTEM PROMPT BODY\n\nModel: test-model\nProvider: openrouter\nPlatform: telegram"


def _agent(flag):
    db = MagicMock()
    db.get_session.return_value = {"system_prompt": STORED}
    agent = MagicMock()
    agent._cached_system_prompt = None
    agent.session_id = "sid-1"
    agent.model, agent.provider, agent.platform = "test-model", "openrouter", "telegram"
    agent._session_db = db
    agent._use_prompt_caching = False
    agent._build_system_prompt = MagicMock(return_value="BUILT_PROMPT")
    agent.enabled_toolsets = agent.disabled_toolsets = None
    agent._restore_stored_system_prompt = flag
    return agent


def test_a_cleared_session_reuses_the_stored_prompt_with_an_empty_history():
    agent = _agent(True)
    _restore_or_build_system_prompt(agent, None, [])
    assert agent._cached_system_prompt == STORED
    agent._build_system_prompt.assert_not_called()


def test_without_the_mark_an_empty_history_still_builds_fresh():
    agent = _agent(False)
    _restore_or_build_system_prompt(agent, None, [])
    assert agent._cached_system_prompt == "BUILT_PROMPT"
    agent._session_db.get_session.assert_not_called()


def test_a_non_boolean_mark_is_not_the_flag():
    agent = _agent(MagicMock())                 # a MagicMock attribute is truthy, not True
    _restore_or_build_system_prompt(agent, None, [])
    assert agent._cached_system_prompt == "BUILT_PROMPT"

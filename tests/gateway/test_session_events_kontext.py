"""WB-1309: the Werkbank chat shows context length, provider and the model of every subagent.

The data rides on the existing events (no new event type):

* ``turn.done`` / ``turn.stopped`` / ``error`` (end of a turn) carry ``provider``, ``context_tokens``
  (the prompt size of the last API call, what ``/usage`` shows), ``context_length`` (the window of the
  active model) and ``compression_threshold`` — read from the turn agent and the gateway result,
  never estimated;
* ``subagent.*`` carry ``model`` AND ``provider`` of the child as it was BUILT (a ``model_profile``
  or a delegation route resolves them after the relay was made).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway import session_events


@pytest.fixture
def emitted(monkeypatch):
    seen = []
    monkeypatch.setattr(session_events, "emit",
                        lambda key, sid, platform, type_, id_=None, parent_id=None, payload=None:
                        seen.append({"type": type_, "id": id_, "parent_id": parent_id, "payload": payload or {}}))
    return seen


def _scope():
    return session_events.TurnScope(session_key="agent:main:telegram:dm:1:2", session_id="s1",
                                    platform="telegram", turn_id="turn_1")


class _Agent:
    def __init__(self, provider="anthropic", model="claude-opus-5-5", tokens=84_000, window=200_000,
                 threshold=160_000):
        self.provider = provider
        self.model = model
        self.context_compressor = SimpleNamespace(last_prompt_tokens=tokens, context_length=window,
                                                  threshold_tokens=threshold)
        self.tool_progress_callback = None
        self.tool_start_callback = None
        self.tool_complete_callback = None
        self.status_callback = None
        self.stream_delta_callback = None
        self.interim_assistant_callback = None


def _result(**over):
    res = {"final_response": "ok", "messages": [], "api_calls": 1, "model": "claude-opus-5-5",
           "last_prompt_tokens": 84_000, "context_length": 200_000}
    res.update(over)
    return res


def test_turn_done_carries_context_and_provider(emitted):
    scope = _scope()
    session_events.wire_agent(scope, _Agent())
    session_events.end_turn(scope, result=_result())
    done = emitted[-1]
    assert done["type"] == "turn.done"
    p = done["payload"]
    assert p["model"] == "claude-opus-5-5"
    assert p["provider"] == "anthropic"
    assert p["context_tokens"] == 84_000
    assert p["context_length"] == 200_000
    assert p["compression_threshold"] == 160_000


def test_context_after_a_compression_is_the_new_lower_number(emitted):
    scope = _scope()
    agent = _Agent()
    session_events.wire_agent(scope, agent)
    session_events.end_turn(scope, result=_result())
    scope2 = _scope()
    session_events.wire_agent(scope2, agent)
    agent.context_compressor.last_prompt_tokens = 21_000
    session_events.end_turn(scope2, result=_result(last_prompt_tokens=21_000))
    assert [e["payload"]["context_tokens"] for e in emitted if e["type"] == "turn.done"] == [84_000, 21_000]


def test_stopped_and_failed_turns_carry_the_context_too(emitted):
    for res, kind in ((_result(interrupted=True, interrupt_message="stop"), "turn.stopped"),
                      (_result(failed=True, error="boom"), "error")):
        scope = _scope()
        session_events.wire_agent(scope, _Agent(provider="openai-codex", model="gpt-5.5"))
        session_events.end_turn(scope, result=res)
        assert emitted[-1]["type"] == kind
        assert emitted[-1]["payload"]["provider"] == "openai-codex"
        assert emitted[-1]["payload"]["context_tokens"] == 84_000


def test_unknown_numbers_stay_absent_never_zero(emitted):
    """No compressor (a test double, an ACP route) and no usage in the result: nothing is
    invented — the fields are left out, so the board says „unbekannt“."""
    scope = _scope()
    agent = _Agent()
    del agent.context_compressor
    session_events.wire_agent(scope, agent)
    session_events.end_turn(scope, result={"final_response": "ok", "messages": [], "api_calls": 1,
                                           "last_prompt_tokens": 0, "context_length": 0})
    p = emitted[-1]["payload"]
    for name in ("context_tokens", "context_length", "compression_threshold"):
        assert name not in p, name
    assert p["provider"] == "anthropic"


def test_turn_without_wired_agent_still_ends_with_the_result_numbers(emitted):
    scope = _scope()
    session_events.end_turn(scope, result=_result())
    p = emitted[-1]["payload"]
    assert p["context_tokens"] == 84_000 and p["context_length"] == 200_000
    assert "provider" not in p and "compression_threshold" not in p


def test_subagent_events_carry_model_and_provider(emitted):
    scope = _scope()
    agent = _Agent()
    session_events.wire_agent(scope, agent)
    agent.tool_progress_callback("subagent.start", None, "goal", None, subagent_id="sa-0-1",
                                 goal="prüfe", model="nemotron-super", provider="nvidia", depth=0)
    start = emitted[-1]
    assert start["type"] == "subagent.start" and start["id"] == "sa-0-1"
    assert start["payload"]["model"] == "nemotron-super"
    assert start["payload"]["provider"] == "nvidia"


def test_turn_end_releases_the_agent_on_every_path(emitted):
    for kind, kw in (("done", dict(result=_result())),
                     ("stopped", dict(result=_result(interrupted=True, interrupt_message="stop"))),
                     ("failed result", dict(result=_result(failed=True, error="boom"))),
                     ("raised error", dict(error=RuntimeError("boom"))),
                     ("emit crashes", dict(result=_result()))):
        scope = _scope()
        session_events.wire_agent(scope, _Agent())
        assert scope.agent is not None, kind
        if kind == "emit crashes":
            scope.emit = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("emit"))
        session_events.end_turn(scope, **kw)
        assert scope.agent is None, kind

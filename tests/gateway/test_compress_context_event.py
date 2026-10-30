"""WB-1319: a manual compression tells the Werkbank chat its new context length at once.

Owner bug: right after ``/compress`` the Werkbank status line still showed the OLD context —
the numbers only travelled on a turn end (``session_events._turn_context``), and a compression is
no turn. Now every committed manual compression (``/compress`` from Telegram or the board, the
board's context editor) emits ``session.state`` with the compressor's own estimate. Plus the
plugin entry ``_compress_from_plugin``: instruction and kept turns reach the compressor, a preview
changes nothing and builds no agent. No model call: the agent is a mock.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _source() -> SessionSource:
    return SessionSource(platform=Platform.TELEGRAM, user_id="u1", chat_id="c1", user_name="tester",
                         chat_type="dm")


def _history():
    rows = []
    for i in range(4):
        rows.append({"role": "user", "content": f"frage {i}"})
        rows.append({"role": "assistant", "content": f"antwort {i}"})
    return rows


def _runner(history):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")})
    entry = SessionEntry(session_key=build_session_key(_source()), session_id="sess-1",
                         created_at=datetime.now(), updated_at=datetime.now(),
                         platform=Platform.TELEGRAM, chat_type="dm")
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store.load_transcript.return_value = history
    runner.session_store.update_session = MagicMock()
    runner.session_store._save = MagicMock()
    runner._session_db = None
    return runner, entry


def _agent(compressed, *, new_id="sess-2"):
    agent = MagicMock()
    agent.context_compressor.has_content_to_compress.return_value = True
    agent.context_compressor.context_length = 200_000
    agent.context_compressor.threshold_tokens = 160_000
    agent.context_compressor._last_compress_aborted = False
    agent.context_compressor._last_summary_error = None
    agent.context_compressor._last_aux_model_failure_model = None
    agent.provider = "anthropic"
    agent.model = "claude-test"
    agent.session_id = new_id
    agent._compress_context.return_value = (compressed, "")
    agent._compression_skipped_due_to_lock = None
    agent._cached_system_prompt = ""
    agent.tools = None
    return agent


def _patches(agent, events):
    def _emit(session_key, session_id, platform, type, id=None, parent_id=None, payload=None, **kw):  # noqa: A002
        events.append({"session_key": session_key, "session_id": session_id, "platform": platform,
                       "type": type, "payload": dict(payload or {})})
        return len(events)

    return (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "***"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent),
        # 1 000 tokens per message: before = 8 000, after = 2 000 (no tokenizer, no model)
        patch("agent.model_metadata.estimate_request_tokens_rough",
              side_effect=lambda messages, **kw: 1000 * len(messages)),
        patch("gateway.session_events.emit", side_effect=_emit),
    )


async def _run(runner, agent, events, coro_factory):
    p = _patches(agent, events)
    with p[0], p[1], p[2] as built, p[3], p[4]:
        persist = MagicMock()

        async def _persist(tmp_agent, entry, source, compressed):
            persist(compressed)
            entry.session_id = tmp_agent.session_id

        runner._persist_manual_compression = _persist
        result = await coro_factory()
        _run.built = built.call_count            # how many agents were constructed
        return result, persist


def _context_events(events):
    return [e for e in events if e["type"] == "session.state"]


@pytest.mark.asyncio
async def test_slash_compress_tells_the_chat_its_new_context_at_once():
    history = _history()
    runner, entry = _runner(history)
    agent = _agent([history[0], history[-1]])
    events = []
    reply, _persist = await _run(runner, agent, events, lambda: runner._handle_compress_command(
        MessageEvent(text="/compress", source=_source(), message_id="m1")))
    assert "🗜" in reply
    found = _context_events(events)
    assert len(found) == 1, events
    ev = found[0]
    assert ev["session_key"] == entry.session_key
    assert ev["session_id"] == "sess-2"                     # the rotated continuation id
    assert ev["platform"] == "telegram"
    assert ev["payload"]["context_tokens"] == 2000          # compress_now's after_tokens
    assert ev["payload"]["context_length"] == 200_000
    assert ev["payload"]["compression_threshold"] == 160_000
    assert ev["payload"]["context_source"] == "kompression"
    assert ev["payload"]["provider"] == "anthropic"


@pytest.mark.asyncio
async def test_a_compression_that_changed_nothing_sends_no_new_context():
    history = _history()
    runner, _entry = _runner(history)
    agent = _agent([history[0], history[-1]])
    agent.context_compressor.has_content_to_compress.return_value = False   # nothing_to_do
    events = []
    await _run(runner, agent, events, lambda: runner._handle_compress_command(
        MessageEvent(text="/compress", source=_source(), message_id="m1")))
    assert _context_events(events) == []


@pytest.mark.asyncio
async def test_plugin_compress_hands_instruction_and_kept_turns_to_the_compressor():
    history = _history()
    runner, entry = _runner(history)
    agent = _agent([{"role": "user", "content": "[Zusammenfassung]"}])
    events = []
    out, persist = await _run(runner, agent, events, lambda: runner._compress_from_plugin(
        source=_source(), focus_topic="nur die Entscheidungen", keep_last=2, preview=False))
    assert out["status"] == "compressed", out
    kwargs = agent._compress_context.call_args.kwargs
    assert kwargs["focus_topic"] == "nur die Entscheidungen"
    assert kwargs.get("verbatim_tail"), "here 2: the last exchanges stay verbatim"
    assert out["before_tokens"] == 8000
    assert out["after_tokens"] == out["context_tokens"] > 0
    assert persist.call_count == 1
    assert len(_context_events(events)) == 1


@pytest.mark.asyncio
async def test_plugin_preview_changes_nothing_and_builds_no_agent():
    history = _history()
    runner, _entry = _runner(history)
    agent = _agent([history[0]])
    events = []
    out, persist = await _run(runner, agent, events, lambda: runner._compress_from_plugin(
        source=_source(), focus_topic="Thema", keep_last=None, preview=True))
    assert out["status"] == "preview", out
    assert out["before_tokens"] > out["after_tokens"] > 0
    assert agent._compress_context.call_count == 0
    assert persist.call_count == 0
    assert runner.session_store.update_session.call_count == 0
    assert _context_events(events) == []
    assert _run.built == 0                                  # no agent, so no model either

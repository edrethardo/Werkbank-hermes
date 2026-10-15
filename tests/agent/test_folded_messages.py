"""WB-1175: a message folded into the running turn (steer, redirect) keeps its own id on the row.

The gateway hands ``steer()``/``redirect()`` a :class:`agent.folded_messages.FoldedText`: the
model-facing text plus, per folded message, ``{ref, text[, internal]}`` (``ref`` = the id the
``user.message`` event announced it under). The row the turn writes for it carries
``display_metadata.folded_messages`` — so a chat surface confirms the announced message by id and
shows the owner's own words instead of the origin preamble or the interruption scaffold.
Every steer/redirect call contributes exactly one part: a plain text without parts becomes a part
of its own, nothing is dropped.
"""
from __future__ import annotations

import threading

from agent.folded_messages import FOLDED_MESSAGES_KEY, FoldedText, folded_parts
from run_agent import AIAgent


def _bare_agent() -> AIAgent:
    agent = object.__new__(AIAgent)
    agent._pending_steer = None
    agent._pending_steer_lock = threading.Lock()
    agent._pending_redirect = None
    agent._pending_redirect_lock = threading.Lock()
    agent._model_request_active = threading.Event()
    agent._executing_tools = False
    agent._execution_thread_id = None
    agent._interrupt_thread_signal_pending = False
    agent._interrupt_requested = False
    agent._interrupt_message = None
    agent._active_children = []
    agent._active_children_lock = threading.Lock()
    agent._tool_worker_threads = None
    agent._tool_worker_threads_lock = None
    agent._current_streamed_assistant_text = ""
    agent._stream_needs_break = False
    agent._strip_think_blocks = lambda content: content
    agent.quiet_mode = True
    agent.api_mode = "chat_completions"
    return agent


def _tool_tail():
    return [{"role": "assistant", "tool_calls": [{"id": "a"}]},
            {"role": "tool", "content": "out", "tool_call_id": "a"}]


def _folded(model_text, ref, own):
    return FoldedText(model_text, [{"ref": ref, "text": own}])


def test_steer_row_names_each_folded_message():
    agent = _bare_agent()
    agent.steer(_folded("Gateway message origin …\n\nAlle todos gelöst", "4728", "Alle todos gelöst"))
    agent.steer(_folded("Gateway message origin …\n\nWer kümmert sich?", "4730", "Wer kümmert sich?"))
    messages = _tool_tail()
    agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
    row = messages[-1]
    assert row["role"] == "user" and row["display_kind"] == "steer"
    assert row["display_metadata"][FOLDED_MESSAGES_KEY] == [
        {"ref": "4728", "text": "Alle todos gelöst"}, {"ref": "4730", "text": "Wer kümmert sich?"}]
    # the model still gets both texts inside the marker
    assert "Alle todos gelöst" in row["content"] and "Wer kümmert sich?" in row["content"]


def test_a_plain_steer_between_folded_ones_is_its_own_part_nothing_dropped():
    agent = _bare_agent()
    agent.steer("plain first")
    agent.steer(_folded("origin\n\nzwei", "m2", "zwei"))
    agent.steer("  plain third  ")
    messages = _tool_tail()
    agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
    assert messages[-1]["display_metadata"][FOLDED_MESSAGES_KEY] == [
        {"ref": None, "text": "plain first"}, {"ref": "m2", "text": "zwei"},
        {"ref": None, "text": "plain third"}]


def test_plain_steers_only_leave_the_row_as_before():
    agent = _bare_agent()
    agent.steer("note")
    messages = _tool_tail()
    agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
    assert "display_metadata" not in messages[-1]


def test_redirect_row_carries_the_ref_and_the_owners_words():
    from agent.conversation_loop import _apply_active_turn_redirect

    agent = _bare_agent()
    agent._model_request_active.set()
    assert agent.redirect(_folded("Gateway message origin …\n\nAlle todos gelöst", "4728", "Alle todos gelöst"))
    text = agent._drain_pending_redirect()
    messages = [{"role": "user", "content": "start"}]
    _apply_active_turn_redirect(agent, messages, text)
    row = messages[-1]
    assert row["role"] == "user"
    assert "[Context from the interrupted assistant response]" in row["api_content"]
    assert row["display_metadata"][FOLDED_MESSAGES_KEY] == [{"ref": "4728", "text": "Alle todos gelöst"}]


def test_two_redirects_keep_both_refs():
    agent = _bare_agent()
    agent._model_request_active.set()
    agent.redirect(_folded("o\n\neins", "a1", "eins"))
    agent.redirect(_folded("o\n\nzwei", "a2", "zwei"))
    assert folded_parts(agent._drain_pending_redirect()) == [
        {"ref": "a1", "text": "eins"}, {"ref": "a2", "text": "zwei"}]


def test_redirect_during_tools_degrades_to_steer_with_its_ref():
    agent = _bare_agent()
    agent._executing_tools = True
    assert agent.redirect(_folded("o\n\nhalt", "r9", "halt"))
    assert folded_parts(agent._drain_pending_steer()) == [{"ref": "r9", "text": "halt"}]


def test_requeued_and_handed_back_steers_keep_their_parts():
    from agent.agent_runtime_helpers import _requeue_pending_steer

    agent = _bare_agent()
    agent.steer(_folded("o\n\neins", "s1", "eins"))
    # no tool result in the batch: the drained text goes back into the slot
    agent._apply_pending_steer_to_tool_results([{"role": "assistant", "content": "x"}], num_tool_msgs=1)
    _requeue_pending_steer(agent, _folded("o\n\nzwei", "s2", "zwei"))
    # the redirect cap hands an unapplied correction back through steer()
    agent.steer(_folded("o\n\ndrei", "s3", "drei"))
    leftover = agent._drain_pending_steer()
    assert folded_parts(leftover) == [{"ref": "s1", "text": "eins"}, {"ref": "s2", "text": "zwei"},
                                      {"ref": "s3", "text": "drei"}]
    assert str(leftover).count("\n") >= 2


def test_internal_part_is_kept_as_marker_only():
    agent = _bare_agent()
    agent.steer(FoldedText("o\n\n[ASYNC DELEGATION BATCH COMPLETE]", [{"ref": None, "internal": True}]))
    messages = _tool_tail()
    agent._apply_pending_steer_to_tool_results(messages, num_tool_msgs=1)
    assert messages[-1]["display_metadata"][FOLDED_MESSAGES_KEY] == [{"ref": None, "internal": True}]

"""WB-1309: the snapshot's ``session`` carries what the last turn end said about the context and
the provider, and every running child its model and provider — so a board that opens the chat
later (or after a new epoch) shows the numbers without waiting for the next turn."""

from __future__ import annotations

from werkbank_testkit import KEY_SESSION, SID, FakeHost, core_event


def _env():
    from plugins.platforms.werkbank import buffer

    host = FakeHost()
    buffers = buffer.Buffers(plugin_id="platforms/werkbank", watermark=host.row_watermark)
    buffers.watch(KEY_SESSION, SID, "telegram")
    return host, buffers


def _snap(host, buffers):
    from plugins.platforms.werkbank import snapshot

    return snapshot.build(buffers, host, KEY_SESSION)


DONE = {"final_response": "ok", "model": "claude-opus-5-5", "provider": "anthropic",
        "context_tokens": 84_000, "context_length": 200_000, "compression_threshold": 160_000}


def test_snapshot_session_has_context_and_provider_of_the_last_turn():
    host, buffers = _env()
    buffers.on_event(core_event(1, "turn.start", {}, id_="t1"))
    buffers.on_event(core_event(2, "turn.done", dict(DONE), id_="t1"))
    s = _snap(host, buffers)["session"]
    assert s["model"] == "claude-opus-5-5"
    assert s["provider"] == "anthropic"
    assert s["context_tokens"] == 84_000
    assert s["context_length"] == 200_000
    assert s["compression_threshold"] == 160_000
    assert s["context_percent"] == 42


def test_a_later_turn_replaces_the_numbers_and_a_new_epoch_keeps_them():
    host, buffers = _env()
    buffers.on_event(core_event(1, "turn.done", dict(DONE), id_="t1"))
    buffers.on_event(core_event(2, "turn.done", dict(DONE, context_tokens=21_000), id_="t2"))
    buffers.new_epoch(KEY_SESSION, "test")
    s = _snap(host, buffers)["session"]
    assert s["context_tokens"] == 21_000
    assert s["context_percent"] == 10


def test_without_numbers_the_snapshot_says_nothing_instead_of_zero():
    host, buffers = _env()
    buffers.on_event(core_event(1, "turn.done", {"final_response": "ok", "model": "m"}, id_="t1"))
    s = _snap(host, buffers)["session"]
    assert s["context_percent"] is None
    assert s.get("context_tokens") is None and s.get("provider") is None


def test_running_child_lists_model_and_provider():
    host, buffers = _env()
    buffers.on_event(core_event(1, "subagent.start", {"goal": "prüfe", "model": "nemotron-3-super",
                                                      "provider": "nvidia"}, id_="sa-0-1"))
    sub = _snap(host, buffers)["subagents"][0]
    assert (sub["model"], sub["provider"]) == ("nemotron-3-super", "nvidia")

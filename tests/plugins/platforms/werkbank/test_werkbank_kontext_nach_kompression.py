"""WB-1319 owner bug: after ``/compress`` the Werkbank status line must show the new context AT ONCE.

The core emits ``session.state`` (``context_source: kompression``) when a manual compression is
committed (gateway/slash_commands_session.py ``_emit_compression_context``). The plugin keeps it in
the snapshot's ``session`` and passes the event on the stream; the next turn end replaces it with
the measured number again.
"""

from __future__ import annotations

from werkbank_testkit import KEY_SESSION, SID, FakeHost, core_event

DONE = {"final_response": "ok", "model": "claude-opus-5-5", "provider": "anthropic",
        "context_tokens": 84_000, "context_length": 200_000, "compression_threshold": 160_000}
KOMPRIMIERT = {"context_tokens": 21_000, "context_length": 200_000, "compression_threshold": 160_000,
               "provider": "anthropic", "context_source": "kompression"}


def _env():
    from plugins.platforms.werkbank import buffer

    host = FakeHost()
    buffers = buffer.Buffers(plugin_id="platforms/werkbank", watermark=host.row_watermark)
    buffers.watch(KEY_SESSION, SID, "telegram")
    return host, buffers


def _snap(host, buffers):
    from plugins.platforms.werkbank import snapshot

    return snapshot.build(buffers, host, KEY_SESSION)


def test_a_compression_replaces_the_old_context_in_the_snapshot_at_once():
    host, buffers = _env()
    buffers.on_event(core_event(1, "turn.done", dict(DONE), id_="t1"))
    buffers.on_event(core_event(2, "session.state", dict(KOMPRIMIERT)))
    s = _snap(host, buffers)["session"]
    assert s["context_tokens"] == 21_000
    assert s["context_percent"] == 10
    assert s["context_source"] == "kompression"
    assert s["provider"] == "anthropic"
    # the board gets the event on the stream too (an open chat needs no reload)
    events = [e for e in buffers._sessions[KEY_SESSION].events if e["type"] == "session.state"]
    assert events and events[-1]["payload"]["context_tokens"] == 21_000


def test_the_next_turn_end_is_the_measured_number_again():
    host, buffers = _env()
    buffers.on_event(core_event(1, "session.state", dict(KOMPRIMIERT)))
    buffers.on_event(core_event(2, "turn.done", dict(DONE, context_tokens=23_500), id_="t2"))
    s = _snap(host, buffers)["session"]
    assert s["context_tokens"] == 23_500
    assert not s.get("context_source")


def test_a_compression_without_a_window_keeps_the_known_window():
    host, buffers = _env()
    buffers.on_event(core_event(1, "turn.done", dict(DONE), id_="t1"))
    buffers.on_event(core_event(2, "session.state", {"context_tokens": 30_000,
                                                     "context_source": "kompression"}))
    s = _snap(host, buffers)["session"]
    assert s["context_tokens"] == 30_000
    assert s["context_length"] == 200_000 and s["provider"] == "anthropic"
    assert s["context_percent"] == 15

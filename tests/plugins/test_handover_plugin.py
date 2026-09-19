"""Tests for the bundled ``handover`` plugin (``plugins/handover/``).

Covers the behaviour contract:
  * ``/handover`` summarizes the live transcript, persists a briefing, and rotates the session.
  * The briefing reaches the NEXT session's first turn through ``pre_llm_call`` — and only once.
  * A failing summarizing model degrades to a mechanical digest instead of losing the handover.
  * The plugin loads through the real discovery path when enabled.
"""

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugins" / "handover"


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    yield hermes_home


def _load_plugin():
    """Import the plugin package fresh (module-level state must not leak between tests)."""
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    for name in [n for n in sys.modules if n.startswith("hermes_plugins.handover")]:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.handover", PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)])
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.handover"
    sys.modules["hermes_plugins.handover"] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeState:
    def __init__(self):
        self.data: Dict[str, Any] = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class _FakeResult:
    def __init__(self, text, model="test-model"):
        self.text = text
        self.model = model


class _FakeLlm:
    def __init__(self, text="## Auftrag\nShip the thing.", exc=None):
        self.text = text
        self.exc = exc
        self.calls: List[Any] = []

    def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.exc is not None:
            raise self.exc
        return _FakeResult(self.text)


class _FakeCli:
    def __init__(self, session_id):
        self.session_id = session_id


class _FakeManager:
    def __init__(self, cli):
        self._cli_ref = cli


class _FakeCtx:
    """Minimal stand-in for PluginContext: only the surface the plugin documents using."""

    def __init__(self, *, session_id="sess-A", llm=None):
        self.state = _FakeState()
        self.llm = llm or _FakeLlm()
        self._manager = _FakeManager(_FakeCli(session_id))
        self.hooks: Dict[str, list] = {}
        self.commands: Dict[str, dict] = {}
        self.injected: List[str] = []

    def register_hook(self, name, cb):
        self.hooks.setdefault(name, []).append(cb)

    def register_command(self, name, handler, description="", args_hint="", **_kw):
        self.commands[name] = {"handler": handler, "description": description, "args_hint": args_hint}

    def inject_message(self, content, role="user", **_kw):
        self.injected.append(content)
        return True


def _registered(session_id="sess-A", llm=None):
    mod = _load_plugin()
    ctx = _FakeCtx(session_id=session_id, llm=llm)
    mod.register(ctx)
    return mod, ctx


_HISTORY = [
    {"role": "system", "content": "you are an agent"},
    {"role": "user", "content": "migrate the deploy script to uv"},
    {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_file"}}]},
    {"role": "tool", "content": "raw tool output that must not reach the summarizer"},
    {"role": "assistant", "content": "Done, scripts/deploy.sh now calls uv sync."},
]


def _run_turn(ctx, session_id, history=_HISTORY, response="Done, scripts/deploy.sh now calls uv sync."):
    """Drive one full turn's worth of hooks and return what pre_llm_call injected."""
    injected = [r for cb in ctx.hooks["pre_llm_call"]
                if (r := cb(session_id=session_id, conversation_history=history)) is not None]
    for cb in ctx.hooks["post_llm_call"]:
        cb(session_id=session_id, conversation_history=history, assistant_response=response)
    return injected


# ---------------------------------------------------------------------------
# transcript normalization (pure core)
# ---------------------------------------------------------------------------

class TestNormalizeTranscript:
    def _core(self):
        _load_plugin()
        return sys.modules["hermes_plugins.handover"].core

    def test_drops_system_and_tool_rows_and_keeps_order(self):
        core = self._core()
        out = core.normalize_transcript(_HISTORY)
        assert [m["role"] for m in out] == ["user", "assistant", "assistant"]
        assert all("raw tool output" not in m["content"] for m in out)

    def test_tool_only_assistant_row_becomes_a_marker(self):
        core = self._core()
        out = core.normalize_transcript(_HISTORY)
        assert out[1]["content"] == "[used tools: read_file]"

    def test_long_message_is_truncated_but_keeps_head_and_tail(self):
        core = self._core()
        body = "A" * 5000 + "TAIL_MARKER"
        out = core.normalize_transcript([{"role": "user", "content": body}])
        assert len(out[0]["content"]) < len(body)
        assert out[0]["content"].startswith("AAA")
        assert out[0]["content"].endswith("TAIL_MARKER")

    def test_multimodal_content_is_flattened(self):
        core = self._core()
        out = core.normalize_transcript([
            {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url"}]}])
        assert out[0]["content"] == "look\n[image]"

    @pytest.mark.parametrize("limit", [40, 200, 2_000, 8_000])
    @pytest.mark.parametrize("size", [41, 300, 10_000, 120_000])
    def test_truncation_never_exceeds_the_limit(self, limit, size):
        """Every cap in this plugin is a budget: the omission marker must fit INSIDE it."""
        core = self._core()
        assert len(core._truncate("X" * size, limit)) <= limit


# ---------------------------------------------------------------------------
# /handover
# ---------------------------------------------------------------------------

class TestHandoverCommand:
    def test_registers_command_and_hooks(self):
        _, ctx = _registered()
        assert "handover" in ctx.commands
        assert set(ctx.hooks) == {"pre_llm_call", "post_llm_call"}

    def test_without_conversation_nothing_is_saved_or_cleared(self):
        mod, ctx = _registered()
        out = mod.handle_slash("")
        assert "Nothing to hand over" in out
        assert ctx.state.get("pending") is None
        assert ctx.injected == []

    def test_summarizes_and_persists_without_touching_the_session(self):
        llm = _FakeLlm("## Auftrag\nMigrate deploy to uv.")
        mod, ctx = _registered(llm=llm)
        _run_turn(ctx, "sess-A")

        out = mod.handle_slash("")

        record = ctx.state.get("pending")
        assert record["text"] == "## Auftrag\nMigrate deploy to uv."
        assert record["source_session_id"] == "sess-A"
        assert record["generated"] is True
        assert "Migrate deploy to uv." in out
        # Freeing context is /compress's job, not this plugin's: nothing is injected,
        # no second summarization is triggered.
        assert ctx.injected == []

    def test_transcript_reaches_the_summarizer_without_tool_rows(self):
        llm = _FakeLlm()
        mod, ctx = _registered(llm=llm)
        _run_turn(ctx, "sess-A")
        mod.handle_slash("")

        prompt = llm.calls[0][0][-1]["content"]
        assert "migrate the deploy script to uv" in prompt
        assert "raw tool output" not in prompt

    def test_focus_text_is_passed_to_the_summarizer(self):
        llm = _FakeLlm()
        mod, ctx = _registered(llm=llm)
        _run_turn(ctx, "sess-A")
        mod.handle_slash("konzentriere dich auf den deploy")

        prompt = llm.calls[0][0][-1]["content"]
        assert "konzentriere dich auf den deploy" in prompt

    def test_show_and_discard(self):
        mod, ctx = _registered()
        _run_turn(ctx, "sess-A")
        mod.handle_slash("")

        assert "## Auftrag" in mod.handle_slash("show")
        assert "discarded" in mod.handle_slash("discard")
        assert ctx.state.get("pending") is None
        assert mod.handle_slash("show") == "No handover is pending."

    def test_help_does_not_touch_state(self):
        mod, ctx = _registered()
        _run_turn(ctx, "sess-A")
        assert mod.handle_slash("help").startswith("/handover")
        assert ctx.state.get("pending") is None


# ---------------------------------------------------------------------------
# delivery into the next session
# ---------------------------------------------------------------------------

class TestDelivery:
    def test_briefing_reaches_the_next_session_first_turn(self):
        """The plugin's entire purpose: survive a session boundary, which in-session
        compaction cannot (ContextCompressor drops its summary on reset)."""
        mod, ctx = _registered()
        _run_turn(ctx, "sess-A")
        mod.handle_slash("")

        injected = _run_turn(ctx, "sess-B", history=[{"role": "user", "content": "weiter"}])
        assert len(injected) == 1
        assert "## Auftrag" in injected[0]["context"]

    def test_briefing_is_delivered_only_once(self):
        mod, ctx = _registered()
        _run_turn(ctx, "sess-A")
        mod.handle_slash("")

        _run_turn(ctx, "sess-B", history=[{"role": "user", "content": "weiter"}])
        again = _run_turn(ctx, "sess-B", history=[{"role": "user", "content": "und nun"}])
        assert again == []
        assert ctx.state.get("pending") is None

    def test_writing_session_never_receives_its_own_briefing(self):
        """/handover clears nothing, so that session still holds the real history and must
        not be handed a summary of context it already has."""
        mod, ctx = _registered()
        _run_turn(ctx, "sess-A")
        mod.handle_slash("")

        assert _run_turn(ctx, "sess-A") == []
        assert ctx.state.get("pending") is not None
        assert len(_run_turn(ctx, "sess-B", history=[{"role": "user", "content": "los"}])) == 1

    def test_injected_block_is_context_not_a_system_prompt_change(self):
        """pre_llm_call returns {'context': ...}: the host appends it to the USER message."""
        mod, ctx = _registered()
        _run_turn(ctx, "sess-A")
        mod.handle_slash("")
        injected = _run_turn(ctx, "sess-B", history=[{"role": "user", "content": "weiter"}])
        assert set(injected[0]) == {"context"}


# ---------------------------------------------------------------------------
# degradation
# ---------------------------------------------------------------------------

class TestDegradation:
    def test_llm_failure_falls_back_to_a_mechanical_digest(self):
        mod, ctx = _registered(llm=_FakeLlm(exc=RuntimeError("provider down")))
        _run_turn(ctx, "sess-A")
        mod.handle_slash("")

        record = ctx.state.get("pending")
        assert record is not None and record["generated"] is False
        assert "migrate the deploy script to uv" in record["text"]

    def test_empty_model_response_falls_back_too(self):
        mod, ctx = _registered(llm=_FakeLlm(text="   "))
        _run_turn(ctx, "sess-A")
        mod.handle_slash("")
        assert ctx.state.get("pending")["generated"] is False

    def test_unwritable_state_does_not_clear_the_session(self):
        mod, ctx = _registered()
        _run_turn(ctx, "sess-A")

        def _boom(key, value):
            raise OSError("read-only filesystem")

        ctx.state.set = _boom
        out = mod.handle_slash("")
        assert "could not be saved" in out

    def test_unreadable_pending_record_is_ignored(self):
        mod, ctx = _registered()
        ctx.state.data["pending"] = {"version": 999, "text": "from the future"}
        assert _run_turn(ctx, "sess-B", history=[{"role": "user", "content": "hi"}]) == []

    def test_credentials_never_reach_the_persisted_briefing(self):
        """The record is written to disk and replayed into a later session, so a secret that
        slipped into the summary must be redacted before it is stored."""
        leaked = "## Kontext\nDeploy with sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        mod, ctx = _registered(llm=_FakeLlm(text=leaked))
        _run_turn(ctx, "sess-A")
        mod.handle_slash("")
        stored = ctx.state.get("pending")["text"]
        assert "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in stored
        assert "Deploy with" in stored  # redaction, not truncation

    def test_oversized_briefing_is_capped(self):
        mod, ctx = _registered(llm=_FakeLlm(text="X" * 50_000))
        _run_turn(ctx, "sess-A")
        mod.handle_slash("")
        core = sys.modules["hermes_plugins.handover"].core
        assert len(ctx.state.get("pending")["text"]) <= core.MAX_HANDOVER_CHARS


# ---------------------------------------------------------------------------
# real discovery path
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_not_loaded_without_opt_in(self, _isolate_env):
        from hermes_cli import plugins as pmod
        mgr = pmod.PluginManager()
        mgr.discover_and_load()
        assert "handover" in mgr._plugins
        assert not mgr._plugins["handover"].enabled

    def test_enabled_plugin_registers_the_slash_command(self, _isolate_env):
        import yaml
        (_isolate_env / "config.yaml").write_text(
            yaml.safe_dump({"plugins": {"enabled": ["handover"]}}))
        from hermes_cli import plugins as pmod
        mgr = pmod.PluginManager()
        mgr.discover_and_load()
        loaded = mgr._plugins["handover"]
        assert loaded.enabled, loaded.error
        assert "handover" in mgr._plugin_commands
        assert "pre_llm_call" in mgr._hooks and "post_llm_call" in mgr._hooks

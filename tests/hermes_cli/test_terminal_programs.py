"""A plugin-registered program shares the chat PTY but none of Hermes' TUI plumbing.

The point of the surface is that the chat page, its input adapter and every later
improvement to them are shared; the child process is the only difference. These
tests pin what makes that safe regardless of WHICH plugin registers: the program
comes from the registry and never from the request, the working directory is the
resolver's decision, and Hermes' own environment does not travel into a foreign
child.

Werkbank-specific behaviour (its launcher, its project register) belongs to the
werkbank_terminal plugin and is tested there, not here.
"""

import os
from pathlib import Path

import pytest

import hermes_cli.main_tui_launch as tui_launch
import hermes_cli.web_server as ws
import hermes_cli.web_server_chat as chat
from hermes_cli import terminal_programs as tp


@pytest.fixture(autouse=True)
def clean_registry():
    """The registry is process-global; never let one test's program leak into the next."""
    for name in list(tp.registered_programs()):
        tp.unregister_program(name)
    yield
    for name in list(tp.registered_programs()):
        tp.unregister_program(name)


@pytest.fixture()
def demo_program():
    """A minimal registered program standing in for any plugin's."""
    def resolve(argument):
        env = tp.scrubbed_child_env()
        if argument:
            env["DEMO_ARGUMENT"] = argument
        return ["/usr/bin/demo", "--run"], None, env

    tp.register_program(tp.TerminalProgram(
        name="demo", title="Demo", resolve=resolve, plugin="demo_plugin"))
    return resolve


def _resolve(monkeypatch, **kwargs):
    monkeypatch.setattr(
        tui_launch, "_make_tui_argv",
        lambda *_a, **_k: (["node", "fake-tui.js"], Path("/tmp")),
    )
    monkeypatch.setattr(ws.app.state, "bound_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(ws.app.state, "bound_port", 9119, raising=False)
    return chat._resolve_chat_argv(**kwargs)


def test_no_program_still_starts_the_hermes_tui(monkeypatch, demo_program):
    argv, _cwd, env = _resolve(monkeypatch)
    assert argv == ["node", "fake-tui.js"]
    assert env["HERMES_TUI_DASHBOARD"] == "1"


def test_a_registered_program_runs_instead_of_the_tui(monkeypatch, demo_program):
    argv, cwd, env = _resolve(monkeypatch, program="demo", project="/home/aaron/code/thing")
    assert argv == ["/usr/bin/demo", "--run"]
    assert env["DEMO_ARGUMENT"] == "/home/aaron/code/thing"
    # The request never chooses a directory; the resolver does.
    assert cwd is None


def test_hermes_tui_environment_does_not_reach_a_foreign_program(monkeypatch, demo_program):
    monkeypatch.setenv("HERMES_TUI_RESUME", "20260915_120000_abcdef")
    monkeypatch.setenv("HERMES_PTY_HOST", "dashboard")
    _argv, _cwd, env = _resolve(monkeypatch, program="demo", project="/tmp/p")
    assert not [k for k in env if k.startswith("HERMES_TUI_")]
    assert "HERMES_PTY_HOST" not in env


def test_an_unregistered_program_is_refused(monkeypatch, demo_program):
    with pytest.raises(tp.UnknownProgramError):
        _resolve(monkeypatch, program="bash", project="/tmp/p")


def test_a_program_disappears_with_the_plugin_that_registered_it(monkeypatch, demo_program):
    assert "demo" in tp.registered_programs()
    tp.unregister_program("demo")
    with pytest.raises(tp.UnknownProgramError):
        _resolve(monkeypatch, program="demo", project="/tmp/p")


def test_an_argument_with_control_characters_is_refused(monkeypatch, demo_program):
    # A trailing \r is the CRLF-smuggling shape: it must be refused, not stripped
    # away and reported clean.
    for bad in ("/tmp/p\nFOO=1", "/tmp/p\0", "/tmp/p\r", "/tmp/a\rb", "/" + "x" * 5000):
        with pytest.raises(tp.UnknownProgramError):
            _resolve(monkeypatch, program="demo", project=bad)


def test_a_resolver_may_refuse_its_own_argument(monkeypatch):
    """Meaning belongs to the plugin: core checks shape, the resolver checks sense."""
    def picky(argument):
        if not argument.startswith("/"):
            raise ValueError("argument must be an absolute path")
        return ["/usr/bin/demo"], None, {}

    tp.register_program(tp.TerminalProgram(
        name="picky", title="Picky", resolve=picky, plugin="demo_plugin"))
    with pytest.raises(ValueError):
        _resolve(monkeypatch, program="picky", project="relative/path")


def test_a_missing_program_says_so_instead_of_spawning(monkeypatch):
    def absent(argument):
        raise FileNotFoundError("Demo: /nowhere/demo is not installed on this host")

    tp.register_program(tp.TerminalProgram(
        name="absent", title="Absent", resolve=absent, plugin="demo_plugin"))
    with pytest.raises(FileNotFoundError):
        _resolve(monkeypatch, program="absent", project="/tmp/p")


def test_a_malformed_name_is_refused_at_registration():
    for bad in ("", "Has Caps", "with/slash", "-leading", "x" * 100, "../escape"):
        with pytest.raises(tp.ProgramNameError):
            tp.register_program(tp.TerminalProgram(
                name=bad, title="Bad", resolve=lambda _a: ([], None, {}), plugin="p"))


def test_one_plugin_cannot_take_over_anothers_program(demo_program):
    """A live name belongs to the plugin that published it."""
    with pytest.raises(tp.ProgramNameError):
        tp.register_program(tp.TerminalProgram(
            name="demo", title="Impostor", resolve=lambda _a: ([], None, {}),
            plugin="other_plugin"))
    # ...while the owner may replace its own entry (a reload).
    tp.register_program(tp.TerminalProgram(
        name="demo", title="Demo v2", resolve=lambda _a: ([], None, {}),
        plugin="demo_plugin"))
    assert tp.registered_programs()["demo"].title == "Demo v2"


def test_an_older_generation_cannot_evict_a_newer_entry(demo_program):
    old = tp.registered_programs()["demo"]
    new = tp.TerminalProgram(name="demo", title="Demo v2",
                             resolve=lambda _a: ([], None, {}), plugin="demo_plugin")
    tp.register_program(new)
    tp.unregister_program("demo", old)  # the old handle's release
    assert tp.registered_programs()["demo"] is new


def test_is_external_distinguishes_a_request_that_asks_for_one():
    assert tp.is_external("") is False
    assert tp.is_external(None) is False
    assert tp.is_external("   ") is False
    assert tp.is_external("demo") is True


def test_the_spawn_size_comes_from_the_browser_and_is_clamped():
    # A foreign TUI redraws its whole intro on SIGWINCH and appends the redraw,
    # so spawning at 80x24 and resizing a moment later stacks the banner.
    assert tp.spawn_size("120", "40") == (120, 40)
    assert tp.spawn_size(None, None) == (80, 24)
    for bad in ("0", "-5", "abc", "", "99999"):
        assert tp.spawn_size(bad, "40") == (80, 40)
        assert tp.spawn_size("120", bad) == (120, 24)


def test_the_child_environment_carries_no_provider_credentials(monkeypatch, demo_program):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-travel")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-travel")
    _argv, _cwd, env = _resolve(monkeypatch, program="demo", project="/tmp/p")
    assert "ANTHROPIC_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env

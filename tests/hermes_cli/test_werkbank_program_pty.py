"""Werkbank's Claude Code shares this PTY but none of Hermes' TUI plumbing.

The point of the feature is that the chat surface, its input adapter and every
later improvement to them are shared; the child process is the only difference.
These tests pin the three things that make that safe: the program comes from a
fixed table, the working directory never comes from the request, and Hermes'
own environment does not travel into a foreign agent.
"""

import os
from pathlib import Path

import pytest

import hermes_cli.main_tui_launch as tui_launch
import hermes_cli.web_server as ws
import hermes_cli.web_server_chat as chat
from hermes_cli import werkbank_programs


@pytest.fixture()
def launcher(tmp_path, monkeypatch):
    script = tmp_path / "werkbank-claude-tui.py"
    script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    monkeypatch.setenv("WERKBANK_TUI_LAUNCHER", str(script))
    return script


def _resolve(monkeypatch, **kwargs):
    monkeypatch.setattr(
        tui_launch, "_make_tui_argv",
        lambda *_a, **_k: (["node", "fake-tui.js"], Path("/tmp")),
    )
    monkeypatch.setattr(ws.app.state, "bound_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(ws.app.state, "bound_port", 9119, raising=False)
    return chat._resolve_chat_argv(**kwargs)


def test_no_program_still_starts_the_hermes_tui(monkeypatch, launcher):
    argv, _cwd, env = _resolve(monkeypatch)
    assert argv == ["node", "fake-tui.js"]
    assert env["HERMES_TUI_DASHBOARD"] == "1"


def test_claude_code_runs_the_werkbank_launcher(monkeypatch, launcher):
    argv, cwd, env = _resolve(
        monkeypatch, program="claude-code", project="/home/aaron/code/agent_ticket")
    assert argv[-1] == str(launcher)
    assert env["WERKBANK_PROJECT"] == "/home/aaron/code/agent_ticket"
    # The request never chooses a directory; the launcher does, from its own
    # register of projects.
    assert cwd is None


def test_hermes_tui_environment_does_not_reach_a_foreign_agent(monkeypatch, launcher):
    monkeypatch.setenv("HERMES_TUI_RESUME", "20260915_120000_abcdef")
    monkeypatch.setenv("HERMES_PTY_HOST", "dashboard")
    _argv, _cwd, env = _resolve(monkeypatch, program="claude-code", project="/tmp/p")
    assert not [k for k in env if k.startswith("HERMES_TUI_")]
    assert "HERMES_PTY_HOST" not in env


def test_an_unlisted_program_is_refused(monkeypatch, launcher):
    with pytest.raises(werkbank_programs.UnknownProgramError):
        _resolve(monkeypatch, program="bash", project="/tmp/p")


def test_a_relative_or_padded_project_is_refused(monkeypatch, launcher):
    for bad in ("relative/path", "/tmp/p\nFOO=1", "/tmp/p\0", "/" + "x" * 5000):
        with pytest.raises(werkbank_programs.UnknownProgramError):
            _resolve(monkeypatch, program="claude-code", project=bad)


def test_a_missing_launcher_says_so_instead_of_spawning(monkeypatch, tmp_path):
    monkeypatch.setenv("WERKBANK_TUI_LAUNCHER", str(tmp_path / "not-installed.py"))
    with pytest.raises(FileNotFoundError):
        _resolve(monkeypatch, program="claude-code", project="/tmp/p")


def test_the_program_table_is_a_fixed_allowlist():
    assert set(werkbank_programs.PROGRAMS) == {"claude-code"}
    assert werkbank_programs.is_external("") is False
    assert werkbank_programs.is_external("claude-code") is True


def test_the_launcher_is_run_with_the_serving_interpreter(monkeypatch, launcher):
    argv, _cwd, _env = _resolve(
        monkeypatch, program="claude-code", project="/tmp/p")
    assert os.path.basename(argv[0]).startswith("python")

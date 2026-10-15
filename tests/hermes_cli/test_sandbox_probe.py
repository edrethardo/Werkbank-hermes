"""``hermes sandbox-probe``: the binary's own report of its EFFECTIVE strict-Docker policy (WB-1090).

Contract (Werkbank ``sandbox_handshake.hermes_probe`` / ``prove_strict``): one JSON object with
``strict`` (True only when strict Docker would really govern tool execution), ``config_sha256``
(sha256 of the config.yaml this profile actually runs with, ``None`` when profile/env/managed
overrides make the effective policy differ from that file), ``docker_network`` and
``auth_exposed`` (both must be ``False`` for a sandbox). A config file alone proves nothing;
these tests pin that the report follows the real config->env bridge, not the file text.

Every test runs in the conftest-isolated HERMES_HOME; nothing starts Docker or a model.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest

STRICT_TERMINAL = {
    "backend": "docker", "docker_strict": True, "docker_image": "python@sha256:" + "a" * 64,
    "cwd": "/workspace", "docker_mount_cwd_to_workspace": False, "docker_run_as_host_user": True,
    "docker_forward_env": [], "docker_env": {"HOME": "/tmp", "PIP_CACHE_DIR": "/cache/pip"},
    "docker_volumes": [], "docker_network": False, "container_cpu": 2, "container_memory": 2048,
    "container_persistent": False, "docker_persist_across_processes": False,
    "docker_shared_container_key": "", "docker_orphan_reaper": False, "docker_extra_args": [],
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "hermes_home"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    for key in list(os.environ):
        if key.startswith("TERMINAL_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    return h


def _write(home, terminal):
    path = home / "config.yaml"
    path.write_text(json.dumps({"terminal": terminal}, indent=2), encoding="utf-8")
    return path


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _probe(path, environ=None):
    from hermes_cli.sandbox_probe import probe

    return probe(path, environ=dict(os.environ) if environ is None else environ)


def test_strict_run_config_is_proven(home):
    path = _write(home, STRICT_TERMINAL)
    rep = _probe(path)
    assert rep["strict"] is True
    assert rep["config_sha256"] == _sha(path)
    assert rep["docker_network"] is False
    assert rep["auth_exposed"] is False
    assert rep["config_matches"] is True
    assert rep["overrides"] == []
    assert rep["backend"] == "docker"


def test_report_satisfies_the_werkbank_contract_exactly(home):
    """Same four checks as Werkbank prove_strict, field for field."""
    path = _write(home, STRICT_TERMINAL)
    rep = _probe(path)
    assert {"strict", "config_sha256", "docker_network", "auth_exposed"} <= set(rep)
    assert rep["strict"] is True and rep["config_sha256"] == _sha(path)
    assert rep["docker_network"] is False and rep["auth_exposed"] is False
    json.dumps(rep)  # must be JSON-serialisable


def test_default_profile_is_not_strict_and_exposes(home):
    path = home / "run.yaml"
    path.write_text("{}", encoding="utf-8")
    rep = _probe(path)
    assert rep["strict"] is False
    assert rep["docker_network"] is True
    assert rep["auth_exposed"] is True
    assert rep["config_sha256"] is None  # no config.yaml governs this profile


def test_env_cannot_turn_off_explicit_file_keys(home):
    """The real bridge lets explicit config.yaml keys win over ambient env."""
    path = _write(home, STRICT_TERMINAL)
    env = dict(os.environ, TERMINAL_DOCKER_STRICT="false", TERMINAL_DOCKER_NETWORK="true",
               TERMINAL_ENV="local")
    rep = _probe(path, env)
    assert rep["strict"] is True and rep["docker_network"] is False
    assert rep["config_sha256"] == _sha(path)


def test_env_filling_an_omitted_key_is_an_override(home):
    terminal = {k: v for k, v in STRICT_TERMINAL.items() if k != "docker_forward_env"}
    path = _write(home, terminal)
    env = dict(os.environ, TERMINAL_DOCKER_FORWARD_ENV='["OPENAI_API_KEY"]')
    rep = _probe(path, env)
    assert "TERMINAL_DOCKER_FORWARD_ENV" in rep["overrides"]
    assert rep["config_sha256"] is None
    assert rep["auth_exposed"] is True


def test_env_network_on_omitted_key_is_reported(home):
    terminal = {k: v for k, v in STRICT_TERMINAL.items() if k != "docker_network"}
    path = _write(home, terminal)
    rep = _probe(path)
    assert rep["docker_network"] is True  # legacy default: network on


def test_ignore_user_config_means_file_not_effective(home):
    path = _write(home, STRICT_TERMINAL)
    rep = _probe(path, dict(os.environ, HERMES_IGNORE_USER_CONFIG="1"))
    assert rep["config_sha256"] is None
    assert rep["strict"] is False


def test_strict_without_docker_backend_is_not_strict(home):
    path = _write(home, dict(STRICT_TERMINAL, backend="local"))
    rep = _probe(path)
    assert rep["strict"] is False
    assert rep["docker_network"] is True
    assert rep["auth_exposed"] is True


def test_non_strict_docker_is_not_strict(home):
    path = _write(home, dict(STRICT_TERMINAL, docker_strict=False))
    rep = _probe(path)
    assert rep["strict"] is False
    assert rep["auth_exposed"] is True  # non-strict Docker auto-injects passthrough/skills


@pytest.mark.parametrize("change", [
    {"docker_forward_env": ["GITHUB_TOKEN"]},
    {"docker_env": {"HOME": "/tmp", "ANTHROPIC_API_KEY": "x"}},
    {"docker_extra_args": ["--privileged"]},
])
def test_explicit_credential_paths_are_exposed(home, change):
    path = _write(home, dict(STRICT_TERMINAL, **change))
    rep = _probe(path)
    assert rep["auth_exposed"] is True
    assert rep["auth_reasons"]


def test_binding_hermes_home_or_ssh_is_exposed(home, tmp_path):
    ssh = tmp_path / "fakehome" / ".ssh"
    ssh.mkdir(parents=True)
    for source in (str(home), str(home.parent), str(home / "sub")):
        path = _write(home, dict(STRICT_TERMINAL, docker_volumes=[f"{source}:/workspace:rw"]))
        assert _probe(path)["auth_exposed"] is True, source
    work = tmp_path / "work"
    work.mkdir()
    path = _write(home, dict(STRICT_TERMINAL, docker_volumes=[f"{work}:/workspace:rw"]))
    assert _probe(path)["auth_exposed"] is False


def test_other_requested_file_does_not_match(home, tmp_path):
    path = _write(home, STRICT_TERMINAL)
    other = tmp_path / "run.yaml"
    other.write_text(json.dumps({"terminal": dict(STRICT_TERMINAL, docker_network=True)}))
    rep = _probe(other)
    assert rep["config_matches"] is False
    assert rep["config_sha256"] == _sha(path) != _sha(other)


def test_unparseable_config_fails_closed(home):
    (home / "config.yaml").write_text("terminal: [unclosed", encoding="utf-8")
    from hermes_cli.sandbox_probe import ProbeError, probe

    with pytest.raises(ProbeError):
        probe(home / "config.yaml", environ=dict(os.environ))


def test_probe_never_mutates_process_env(home):
    path = _write(home, STRICT_TERMINAL)
    before = {k: v for k, v in os.environ.items() if k.startswith("TERMINAL_")}
    _probe(path)
    assert {k: v for k, v in os.environ.items() if k.startswith("TERMINAL_")} == before


def _main(monkeypatch, capsys, argv):
    import hermes_cli.main as main_mod

    monkeypatch.setattr(sys, "argv", ["hermes", *argv])
    try:
        main_mod.main()
        code = 0
    except SystemExit as e:
        code = e.code or 0
    return code, capsys.readouterr()


def test_cli_json(home, monkeypatch, capsys):
    path = _write(home, STRICT_TERMINAL)
    code, out = _main(monkeypatch, capsys, ["sandbox-probe", "--config", str(path), "--json"])
    assert code == 0, out.err
    rep = json.loads(out.out)
    assert rep["strict"] is True and rep["config_sha256"] == _sha(path)


def test_cli_missing_config_file_exits_nonzero(home, monkeypatch, capsys):
    code, out = _main(monkeypatch, capsys,
                      ["sandbox-probe", "--config", str(home / "nope.yaml"), "--json"])
    assert code != 0


def test_cli_unparseable_config_exits_nonzero(home, monkeypatch, capsys):
    (home / "config.yaml").write_text("terminal: [unclosed", encoding="utf-8")
    code, out = _main(monkeypatch, capsys,
                      ["sandbox-probe", "--config", str(home / "config.yaml"), "--json"])
    assert code != 0


def test_probe_is_builtin_so_no_plugin_code_runs(monkeypatch):
    """A probe that imported plugins first would report on a process plugins could change."""
    import hermes_cli.main as main_mod

    monkeypatch.setattr(sys, "argv", ["hermes", "-p", "werkbank", "sandbox-probe", "--config", "x"])
    assert main_mod._plugin_cli_discovery_needed() is False

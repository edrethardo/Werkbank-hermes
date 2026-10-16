"""WB-1198: ``sandbox-probe`` names the Docker daemon of the run and whether it is rootless.

The Werkbank may demand a rootless daemon (``hermes_sandbox.require_rootless``). Only the binary
can say which daemon its run talks to: the report carries ``docker_host`` (the run config's
``terminal.docker_host``, ``None`` = the CLI default) and ``rootless`` (``docker info`` of THAT
daemon: True/False, ``None`` when it cannot be asked). A run config with a non-unix
``docker_host`` is refused like every other invalid strict config. Docker is faked.
"""
from __future__ import annotations

import json
import os

import pytest

HOST = "unix:///run/user/1000/docker.sock"


def _strict_terminal(ws, cache, **extra):
    return {
        "backend": "docker", "docker_strict": True, "docker_image": "python@sha256:" + "a" * 64,
        "cwd": "/workspace", "docker_mount_cwd_to_workspace": False, "docker_run_as_host_user": True,
        "docker_forward_env": [], "docker_env": {"HOME": "/tmp"},
        "docker_volumes": [f"{ws}:/workspace:rw", f"{cache}:/cache:rw"], "docker_network": False,
        "container_cpu": 2, "container_memory": 2048, "container_persistent": False,
        "docker_persist_across_processes": False, "docker_shared_container_key": "",
        "docker_orphan_reaper": False, "container_disk": 0, "docker_extra_args": [], **extra,
    }


@pytest.fixture
def lauf(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in list(os.environ):
        if key.startswith("TERMINAL_") or key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
                                                  "HERMES_DOCKER_BINARY"):
            monkeypatch.delenv(key, raising=False)
    root = tmp_path / "lauf"
    (root / "workspace").mkdir(parents=True)
    (root / "cache").mkdir()
    yield {"home": home, "root": root}
    import hermes_cli.sandbox_config as sandbox_config
    sandbox_config._reset_for_tests()


@pytest.fixture
def asked(monkeypatch):
    """Fake ``docker_rootless``: records which daemon was asked, answers per daemon."""
    from tools.environments import docker
    seen = []
    answers = {HOST: True, "": False}

    def fake(docker_host=""):
        seen.append(docker_host)
        return answers.get(docker_host)
    monkeypatch.setattr(docker, "docker_rootless", fake)
    return {"seen": seen, "answers": answers}


def _config(lauf, **extra):
    path = lauf["root"] / "config.yaml"
    path.write_text(json.dumps({"terminal": _strict_terminal(lauf["root"] / "workspace",
                                                             lauf["root"] / "cache", **extra)},
                               indent=2), encoding="utf-8")
    return path


def test_sandbox_config_probe_names_the_daemon_and_rootless(lauf, asked):
    from hermes_cli.sandbox_probe import probe_sandbox_config
    rep = probe_sandbox_config(_config(lauf, docker_host=HOST))
    assert rep["docker_host"] == HOST
    assert rep["rootless"] is True
    assert asked["seen"] == [HOST]
    assert rep["config_matches"] is True and rep["overrides"] == []
    assert rep["auth_exposed"] is False, rep["auth_reasons"]


def test_sandbox_config_probe_default_daemon(lauf, asked):
    from hermes_cli.sandbox_probe import probe_sandbox_config
    rep = probe_sandbox_config(_config(lauf))
    assert rep["docker_host"] is None
    assert rep["rootless"] is False
    assert asked["seen"] == [""]


def test_unreachable_daemon_is_unknown_not_rootless(lauf, asked):
    from hermes_cli.sandbox_probe import probe_sandbox_config
    asked["answers"].pop(HOST)
    rep = probe_sandbox_config(_config(lauf, docker_host=HOST))
    assert rep["rootless"] is None


@pytest.mark.parametrize("bad", ["tcp://10.0.0.9:2375", "unix://relative.sock"])
def test_run_config_with_a_network_daemon_is_refused(lauf, asked, bad):
    from hermes_cli.sandbox_probe import ProbeError, probe_sandbox_config
    with pytest.raises(ProbeError, match="docker_host"):
        probe_sandbox_config(_config(lauf, docker_host=bad))


def test_docker_host_changes_the_config_hash(lauf, asked):
    from hermes_cli.sandbox_probe import probe_sandbox_config
    plain = probe_sandbox_config(_config(lauf))["config_sha256"]
    rootless = probe_sandbox_config(_config(lauf, docker_host=HOST))["config_sha256"]
    assert plain and rootless and plain != rootless


def test_profile_probe_reports_daemon_too(lauf, asked):
    from hermes_cli.sandbox_probe import probe
    path = lauf["home"] / "config.yaml"
    path.write_text(json.dumps({"terminal": _strict_terminal(lauf["root"] / "workspace",
                                                             lauf["root"] / "cache",
                                                             docker_host=HOST)}), encoding="utf-8")
    rep = probe(path, environ=dict(os.environ))
    assert rep["docker_host"] == HOST and rep["rootless"] is True


def test_cli_text_output_names_daemon_and_rootless(lauf, asked, capsys):
    from types import SimpleNamespace

    from hermes_cli.sandbox_probe import cmd_sandbox_probe
    rc = cmd_sandbox_probe(SimpleNamespace(json=False, sandbox_config=str(_config(lauf, docker_host=HOST)),
                                           config=None))
    out = capsys.readouterr().out
    assert rc == 0
    assert f"docker_host:      {HOST}" in out
    assert "rootless:         yes" in out

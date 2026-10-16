"""WB-1198: ``terminal.docker_host`` selects the Docker daemon explicitly (e.g. a rootless one).

The Werkbank runs sandbox containers on a second, rootless daemon of the owner instead of the
root daemon. Its run config names that daemon as ``terminal.docker_host`` (a ``unix:///`` socket
only); EVERY docker CLI call Hermes makes for that environment goes ``docker --host <it> ...``
(which beats ``DOCKER_HOST`` and the CLI context), and the value never becomes a container
variable. ``docker_rootless`` asks THAT daemon whether it runs rootless. Docker is faked;
nothing starts.
"""
import subprocess

import pytest

from tools import docker_strict
from tools.environments import docker

HOST = "unix:///run/user/1000/docker.sock"


@pytest.fixture
def docker_calls(monkeypatch):
    calls = []

    def capture(argv, **kwargs):
        calls.append(list(argv))
        stdout = "null" if "inspect" in argv else "rootless-test-id"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    def run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    class _Proc:
        pid = 1
        returncode = 0

    def popen(cmd, *a, **k):
        calls.append(list(cmd))
        return _Proc()

    monkeypatch.setattr(docker, "run_capture", capture)
    monkeypatch.setattr(docker.subprocess, "run", run)
    monkeypatch.setattr(docker, "_popen_bash", popen)
    monkeypatch.setattr(docker, "find_docker", lambda: "docker")
    monkeypatch.setattr(docker, "_resolve_host_user_spec", lambda: "1234:1234")
    monkeypatch.setattr(docker, "_cgroup_limits_ok", True)
    monkeypatch.setattr(docker, "_cgroup_limits_by_host", {})
    monkeypatch.setattr(docker.DockerEnvironment, "init_session", lambda self: None)
    return calls


def _env_names(argv):
    return [argv[i + 1].split("=", 1)[0] for i, a in enumerate(argv) if a in ("-e", "--env")]


# --- validate_docker_host -------------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, ""])
def test_no_docker_host_is_the_default_daemon(value):
    assert docker_strict.validate_docker_host(value) == ""


def test_unix_socket_is_accepted():
    assert docker_strict.validate_docker_host(HOST) == HOST


@pytest.mark.parametrize("value", [
    "tcp://127.0.0.1:2375", "ssh://aaron@host", "npipe:////./pipe/docker", "unix://run/docker.sock",
    "unix:///run/../etc/docker.sock", "unix:///run//docker.sock", "unix:///run/./docker.sock",
    "unix:///run/docker sock", "unix:///run/docker.sock\n", "/run/docker.sock", "unix:///", 5,
    ["unix:///x"],
])
def test_anything_but_a_normalized_unix_socket_is_rejected(value):
    with pytest.raises(ValueError, match="docker_host"):
        docker_strict.validate_docker_host(value)


# --- DockerEnvironment wiring ---------------------------------------------------------------------

@pytest.mark.parametrize("strict", [True, False])
def test_every_docker_call_goes_to_the_configured_daemon(docker_calls, strict):
    from tools.environments.streams import exec_prefix

    env = docker.DockerEnvironment("test-image", strict=strict, docker_host=HOST,
                                   persist_across_processes=False)
    env._run_bash("true")
    stream = exec_prefix(env)
    env.cleanup(force_remove=True)
    env.wait_for_cleanup(5)
    assert docker_calls, "no docker call recorded"
    assert [c for c in docker_calls if c[:3] != ["docker", "--host", HOST]] == []
    assert stream[:3] == ["docker", "--host", HOST]
    assert any(c[3:5] == ["run", "-d"] for c in docker_calls)
    assert any(c[3:4] == ["exec"] for c in docker_calls)
    assert any(c[3:4] == ["rm"] for c in docker_calls)


def test_without_docker_host_argv_is_unchanged(docker_calls):
    env = docker.DockerEnvironment("test-image", strict=True)
    env._run_bash("true")
    assert docker_calls
    assert not [c for c in docker_calls if "--host" in c]
    assert all(c[0] == "docker" for c in docker_calls)


def test_docker_host_never_becomes_a_container_variable(docker_calls, monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    monkeypatch.setenv("TERMINAL_DOCKER_HOST", HOST)
    docker.DockerEnvironment("test-image", strict=True, docker_host=HOST,
                             env={"HOME": "/tmp"})
    run = next(c for c in docker_calls if c[3:5] == ["run", "-d"])
    names = _env_names(run)
    assert "DOCKER_HOST" not in names and "TERMINAL_DOCKER_HOST" not in names
    assert not [a for a in run if "DOCKER_HOST=" in a]


@pytest.mark.parametrize("value", ["tcp://10.0.0.9:2375", "unix://relative.sock"])
def test_invalid_docker_host_never_reaches_docker(docker_calls, value):
    with pytest.raises(ValueError, match="docker_host"):
        docker.DockerEnvironment("test-image", strict=True, docker_host=value)
    assert docker_calls == []


def test_podman_cannot_take_a_docker_host(docker_calls, monkeypatch):
    monkeypatch.setattr(docker, "find_docker", lambda: "/usr/bin/podman")
    with pytest.raises(ValueError, match="podman"):
        docker.DockerEnvironment("test-image", strict=True, docker_host=HOST)
    assert docker_calls == []


def test_cgroup_probe_result_is_per_daemon(docker_calls, monkeypatch):
    """The root daemon's cached verdict says nothing about the rootless one (no delegated
    controllers is the classic rootless failure): the probe runs on THAT daemon."""
    docker.DockerEnvironment("test-image", strict=True, docker_host=HOST)
    probes = [c for c in docker_calls if "--cpus" in c and "--rm" in c]
    assert probes and probes[0][:3] == ["docker", "--host", HOST]


# --- config bridge --------------------------------------------------------------------------------

def test_config_key_reaches_the_environment_kwargs(monkeypatch):
    import tools.terminal_tool as terminal_tool
    import tools.terminal_tool_backends as backends
    from hermes_cli.config import TERMINAL_CONFIG_ENV_MAP
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["terminal"]["docker_host"] == ""
    assert TERMINAL_CONFIG_ENV_MAP["docker_host"] == "TERMINAL_DOCKER_HOST"
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_DOCKER_HOST", HOST)
    cfg = terminal_tool._get_env_config()
    assert cfg["docker_host"] == HOST
    cc = backends._container_config_from_config(cfg)
    assert cc["docker_host"] == HOST
    assert ("docker_host", "docker_host", "") in backends._DOCKER_KWARGS


def test_cli_and_gateway_bridges_know_the_key():
    import inspect

    import gateway.run as gateway_run
    from hermes_cli import cli_config_load

    assert "docker_host" in str(cli_config_load._TERMINAL_ENV_MAPPINGS)
    assert '"docker_host": "TERMINAL_DOCKER_HOST"' in inspect.getsource(
        gateway_run._bridge_terminal_config_to_env)


# --- rootless report ------------------------------------------------------------------------------

@pytest.mark.parametrize("options,expected", [
    ('["name=seccomp,profile=builtin","name=rootless","name=cgroupns"]', True),
    ('["name=apparmor,profile=default","name=seccomp,profile=builtin","name=cgroupns"]', False),
])
def test_docker_rootless_asks_the_selected_daemon(monkeypatch, options, expected):
    seen = []

    def capture(argv, **kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=options + "\n", stderr="")
    monkeypatch.setattr(docker, "run_capture", capture)
    monkeypatch.setattr(docker, "find_docker", lambda: "docker")
    assert docker.docker_rootless(HOST) is expected
    assert seen == [["docker", "--host", HOST, "info", "--format", "{{json .SecurityOptions}}"]]


@pytest.mark.parametrize("result", ["fail", "garbage", "raise"])
def test_docker_rootless_unknown_is_none(monkeypatch, result):
    def capture(argv, **kwargs):
        if result == "raise":
            raise subprocess.TimeoutExpired(argv, 10)
        return subprocess.CompletedProcess(argv, 1 if result == "fail" else 0,
                                           stdout="nope", stderr="Cannot connect")
    monkeypatch.setattr(docker, "run_capture", capture)
    monkeypatch.setattr(docker, "find_docker", lambda: "docker")
    assert docker.docker_rootless(HOST) is None


# --- bot desktop marker: a restarted gateway checks the container on ITS daemon ---------------------

def test_bot_desktop_marker_keeps_the_daemon(monkeypatch):
    from types import SimpleNamespace

    from tools.bot_desktop import sandbox_host

    env = SimpleNamespace(_container_id="c1", _docker_exe="docker", _docker_host=HOST)
    ident = sandbox_host._owner_identity(env)
    assert ident["docker_host"] == HOST
    seen = []

    def run(argv, **kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="true\n", stderr="")
    monkeypatch.setattr(sandbox_host.subprocess, "run", run)
    monkeypatch.setattr(sandbox_host, "_ALIVE_CACHE", {})
    assert sandbox_host.marker_sandbox_alive(ident) is True
    assert seen[0][:3] == ["docker", "--host", HOST]
    plain = sandbox_host._owner_identity(SimpleNamespace(_container_id="c2", _docker_exe="docker"))
    assert "docker_host" not in plain

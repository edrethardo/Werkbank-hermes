"""WB855: opt-in Docker confinement, real dispatch and profile-policy wiring."""
import json
import socket
import subprocess
from types import SimpleNamespace

import pytest

from tools.environments import docker
from tools.docker_strict import validate_volumes, tool_denial
from tools.terminal_scope import build_profile_terminal_scope, set_terminal_scope, reset_terminal_scope


@pytest.fixture
def strict_scope():
    token = set_terminal_scope({"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_STRICT": "true"})
    yield
    reset_terminal_scope(token)


@pytest.fixture
def docker_calls(monkeypatch):
    calls = []
    def capture(argv, **kwargs):
        calls.append((argv, kwargs))
        stdout = "null" if "inspect" in argv else "strict-test-id"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")
    monkeypatch.setattr(docker, "run_capture", capture)
    monkeypatch.setattr(docker, "find_docker", lambda: "docker")
    monkeypatch.setattr(docker, "_resolve_host_user_spec", lambda: "1234:1234")
    monkeypatch.setattr(docker, "_cgroup_limits_ok", True)
    monkeypatch.setattr(docker.DockerEnvironment, "init_session", lambda self: None)
    return calls


def test_strict_only_explicit_mounts_env_and_nonroot(docker_calls, tmp_path, monkeypatch):
    def forbidden(*a, **k):
        pytest.fail("implicit host access")
    monkeypatch.setattr(docker, "_readonly_skill_mount_args", forbidden)
    monkeypatch.setattr(docker, "_egress_proxy_args_for_docker", forbidden)
    monkeypatch.setattr(docker, "_load_hermes_env_vars", forbidden)
    monkeypatch.setattr(docker.DockerEnvironment, "_attach_existing_container", forbidden)
    monkeypatch.setenv("WB855_IMPLICIT_SECRET", "dummy-not-a-credential")
    env = docker.DockerEnvironment("test-image", strict=True, volumes=[f"{tmp_path}:/workspace:rw"],
        env={"EXPLICIT": "configured"}, persistent_filesystem=True, auto_mount_cwd=True,
        host_cwd=str(tmp_path), run_as_host_user=False, persist_across_processes=True)
    argv = next(argv for argv, _ in docker_calls if "run" in argv)
    assert [argv[i+1] for i, arg in enumerate(argv) if arg == "-v"] == [f"{tmp_path}:/workspace:rw"]
    assert argv[argv.index("--user")+1] == "1234:1234"
    assert "--cap-add" not in argv and "no-new-privileges" in argv
    assert "--cpus" in argv and "--memory" in argv and "--pids-limit" in argv
    assert not env._persist_across_processes and env.host_cwd is None
    assert env._run_env_values == {"EXPLICIT": "configured"}
    assert env._init_env_values == {"EXPLICIT": "configured"}
    assert env._build_runtime_env_args_with_unsets() == ([], (), {})
    assert not any("/root:" in arg or "/home:" in arg for arg in argv)


def test_strict_cgroup_failure_never_starts_workload(docker_calls, monkeypatch):
    monkeypatch.setattr(docker, "_cgroup_limits_ok", False)
    with pytest.raises(RuntimeError, match="refusing container start"):
        docker.DockerEnvironment("test-image", strict=True)
    assert not any("run" in argv or "start" in argv for argv, _ in docker_calls)


@pytest.mark.parametrize("extra", [
    ["--privileged"], ["--pid=host"], ["--ipc", "host"], ["--network=host"],
    ["--device=/dev/sda"], ["--volumes-from", "other"], ["-v", "/:/host"],
    ["--mount=type=bind,source=/run,target=/run"], ["--user=0"],
    ["--env-file=/secret"], ["--cap-add=SYS_ADMIN"], ["--security-opt=seccomp=unconfined"],
    ["--entrypoint=sh"], ["--cgroupns=host"], ["--shm-size=2g"],
])
def test_strict_rejects_all_extra_args_before_docker(docker_calls, extra):
    with pytest.raises(ValueError, match="docker_extra_args"):
        docker.DockerEnvironment("test-image", strict=True, extra_args=extra)
    assert not docker_calls


@pytest.mark.parametrize("volume", ["named:/workspace", "/:/host", "/run:/run", "/var:/host", "/proc:/host", "/dev:/host"])
def test_strict_rejects_parent_and_special_mounts(volume):
    with pytest.raises(ValueError):
        validate_volumes([volume])


def test_strict_socket_indirection_and_valid_bind(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "regular").write_text("dummy", encoding="utf-8")
    assert validate_volumes([f"{source}:/workspace:ro"]) == [f"{source}:/workspace:ro"]
    sock = socket.socket(socket.AF_UNIX)
    try:
        monkeypatch.chdir(source)
        sock.bind("renamed.sock")
        link = tmp_path / "alias"
        link.symlink_to(source, target_is_directory=True)
        for path in (source, link, source / "renamed.sock", tmp_path):
            with pytest.raises(ValueError, match="sockets"):
                validate_volumes([f"{path}:/workspace"])
    finally:
        sock.close()


@pytest.mark.parametrize("user", [None, "0:0"])
def test_strict_requires_nonroot_host_identity(docker_calls, monkeypatch, user):
    monkeypatch.setattr(docker, "_resolve_host_user_spec", lambda: user)
    with pytest.raises(ValueError, match="non-root POSIX"):
        docker.DockerEnvironment("test-image", strict=True)
    assert not docker_calls


def test_strict_image_volumes_fail_closed(docker_calls, monkeypatch):
    monkeypatch.setattr(docker, "run_capture", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout='{"/data": {}}', stderr=""))
    with pytest.raises(ValueError, match="VOLUME"):
        docker.DockerEnvironment("test-image", strict=True)


def test_default_keeps_legacy_automatic_mount_and_env(docker_calls, monkeypatch):
    monkeypatch.setattr(docker, "_readonly_skill_mount_args", lambda: ["-v", "/dummy:/credential:ro"])
    monkeypatch.setattr(docker, "resolve_passthrough_env", lambda *a, **kw: ({"LEGACY": "dummy"}, set()))
    env = docker.DockerEnvironment("test-image", persist_across_processes=False)
    assert "/dummy:/credential:ro" in env._all_run_args
    assert env._init_env_values["LEGACY"] == "dummy"
    assert "--user" not in env._all_run_args


def test_every_host_tool_denied_through_dispatchers(strict_scope):
    import model_tools
    from tools.registry import registry
    # New tools are denied by default, including plugins, MCP, bridge wrappers
    # and agent-inline names. No individual tool handler is invoked.
    names = set(registry.get_all_tool_names()) | {
        "read_file", "execute_code", "browser_navigate", "web_search", "web_extract",
        "memory", "todo", "delegate_task", "tool_call", "tool_describe", "tool_search",
        "future_plugin_tool", "connectors__test__read", "process_manage",
    }
    for name in names - {"terminal"}:
        assert "docker_strict" in json.loads(model_tools.handle_function_call(name, {}))["error"]
        assert "docker_strict" in json.loads(registry.dispatch(name, {}))["error"]
    definitions = model_tools.get_tool_definitions(quiet_mode=True)
    assert {d["function"]["name"] for d in definitions} <= {"terminal"}


def test_direct_file_code_and_agent_inline_paths_denied(strict_scope, tmp_path):
    from tools.file_tools import read_file_tool
    from tools.code_execution_tool import execute_code
    from agent.agent_runtime_helpers import invoke_tool
    from agent.tool_executor import _resolve_sequential_dispatch
    target = tmp_path / "canary"
    target.write_text("UNIQUE-HOST-CANARY", encoding="utf-8")
    result = read_file_tool(str(target))
    assert "UNIQUE-HOST-CANARY" not in result and "docker_strict" in result
    assert "docker_strict" in execute_code("raise Exception('must not execute')")
    assert "docker_strict" in invoke_tool(None, "memory", {}, "strict-test")
    ref = SimpleNamespace(name="memory", args={}, task_id="strict-test", call_id="call", trace=[])
    assert "docker_strict" in _resolve_sequential_dispatch(None, ref, []).execute({})


def test_config_real_profiles_a_b_a_and_backend_wiring(tmp_path, monkeypatch, docker_calls):
    from tools.terminal_tool import _get_env_config
    from tools.terminal_tool_backends import _container_config_from_config, _build_docker_env
    homes = []
    for name, strict in [("a", "true"), ("b", "false")]:
        home = tmp_path / name
        home.mkdir()
        (home / "config.yaml").write_text(f"terminal:\n  backend: docker\n  docker_strict: {strict}\n  container_disk: 0\n", encoding="utf-8")
        homes.append(home)
    for home, expected in [(homes[0], True), (homes[1], False), (homes[0], True)]:
        token = set_terminal_scope(build_profile_terminal_scope(home))
        try:
            cfg = _get_env_config()
            assert cfg["docker_strict"] is expected
            assert bool(tool_denial("read_file")) is expected
            if expected:
                env = _build_docker_env(image="test-image", cwd="/tmp", timeout=30,
                    cc=_container_config_from_config(cfg), task_id="strict-test", host_cwd=None)
                assert env._strict and not env._persist_across_processes
        finally:
            reset_terminal_scope(token)


def test_terminal_rejects_host_override_background_and_cached_local(strict_scope, monkeypatch):
    import tools.terminal_tool as terminal
    for args in ({"_host_local": True}, {"background": True}, {"pty": True}):
        assert "docker_strict" in terminal.terminal_tool("true", **args)
    monkeypatch.setattr(terminal, "_plan_execution", lambda *a, **kw: SimpleNamespace(
        env_type="docker", promoted_from_foreground_timeout=None))
    monkeypatch.setattr(terminal, "_acquire_env", lambda *a, **kw: object())
    assert "cached non-strict" in terminal.terminal_tool("true")


def test_strict_probe_is_hardened_and_timeout_is_cleaned(docker_calls, monkeypatch):
    monkeypatch.setattr(docker, "_cgroup_limits_ok", None)
    calls = []
    def capture(argv, **kwargs):
        calls.append(argv)
        if "run" in argv:
            raise subprocess.TimeoutExpired(argv, 1)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(docker, "run_capture", capture)
    assert docker._cgroup_limits_available("test-image", strict=True) is False
    run, cleanup = calls
    assert run[run.index("--user") + 1] == "1234:1234"
    assert "--cap-drop" in run and "no-new-privileges" in run
    assert run[run.index("--network") + 1] == "none"
    assert run[run.index("--entrypoint") + 1] == "sleep"
    assert cleanup == ["docker", "rm", "-f", run[run.index("--name") + 1]]


def test_explicit_forward_names_only(docker_calls, monkeypatch):
    monkeypatch.setenv("WB855_EXPLICIT", "dummy-explicit")
    monkeypatch.setenv("WB855_IMPLICIT", "dummy-implicit")
    env = docker.DockerEnvironment("test-image", strict=True, forward_env=["WB855_EXPLICIT"])
    assert env._init_env_values == {"WB855_EXPLICIT": "dummy-explicit"}
    args, absent, values = env._build_runtime_env_args_with_unsets()
    assert args == ["-e", "WB855_EXPLICIT"] and not absent
    assert values == {"WB855_EXPLICIT": "dummy-explicit"}


def test_strict_backend_mismatch_never_falls_back():
    token = set_terminal_scope({"TERMINAL_ENV": "local", "TERMINAL_DOCKER_STRICT": "true"})
    try:
        from tools.terminal_tool import terminal_tool
        assert "no host fallback" in terminal_tool("true")
    finally:
        reset_terminal_scope(token)

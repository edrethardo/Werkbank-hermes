"""WB-1177: strict Docker gets ONE board socket and run labels, nothing else.

The Werkbank board door (WB-1065) is a single Unix socket ``<run>/board.sock``; its reaper
(WB-1066) finds containers only by ``werkbank.sandbox.run=<run>``. ``docker_volumes`` keeps
rejecting every socket, so the door gets its own key with its own, narrower rules:
``terminal.docker_board_socket`` (exactly one caller-owned socket, private directory, never a
Docker/Podman socket, target under ``/run/werkbank/``) and ``terminal.docker_labels`` (only the
``werkbank.`` prefix, label-safe values, at most four). Docker is faked; nothing starts.
"""
import os
import socket
import subprocess

import pytest

from tools import docker_strict
from tools.docker_strict import validate_volumes
from tools.environments import docker

TARGET = "/run/werkbank/board.sock"


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


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    """A Werkbank-like run root: private (0700) directory owned by the caller."""
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    return root


@pytest.fixture
def bind_socket(monkeypatch):
    """Bind a Unix socket relative to its directory (fixture paths can exceed sun_path)."""
    opened = []

    def bind(directory, name="board.sock", mode=0o600):
        sock = socket.socket(socket.AF_UNIX)
        monkeypatch.chdir(directory)
        sock.bind(name)
        os.chmod(directory / name, mode)
        opened.append(sock)
        return directory / name
    yield bind
    for sock in opened:
        sock.close()


def _run_argv(calls):
    return next(argv for argv, _ in calls if "run" in argv and "-d" in argv)


def _flag_values(argv, flag):
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == flag]


# --- validate_board_socket ------------------------------------------------------------------------

@pytest.mark.parametrize("spec", [None, ""])
def test_no_board_socket_is_nothing(spec):
    assert docker_strict.validate_board_socket(spec) == []


def test_valid_board_socket(run_dir, bind_socket):
    sock = bind_socket(run_dir)
    assert docker_strict.validate_board_socket(f"{sock}:{TARGET}") == [f"{sock}:{TARGET}:rw"]


def test_socket_mode_0700_is_accepted(run_dir, bind_socket):
    """Werkbank binds under umask 077, which yields 0700 on the socket inode."""
    sock = bind_socket(run_dir, mode=0o700)
    assert docker_strict.validate_board_socket(f"{sock}:{TARGET}") == [f"{sock}:{TARGET}:rw"]


@pytest.mark.parametrize("spec", [
    ["/a/board.sock:/run/werkbank/board.sock", "/b/board.sock:/run/werkbank/board.sock"],
    ["/a/board.sock:/run/werkbank/board.sock"],
    {"source": "/a/board.sock"},
    "/a/board.sock:/run/werkbank/board.sock,/b/board.sock:/run/werkbank/other.sock",
    "/a/board.sock:/run/werkbank/board.sock:/b/board.sock:/run/werkbank/other.sock",
])
def test_exactly_one_socket_spec(spec):
    with pytest.raises(ValueError, match="exactly one"):
        docker_strict.validate_board_socket(spec)


def test_second_socket_via_volumes_is_still_rejected(run_dir, bind_socket, tmp_path):
    sock = bind_socket(run_dir)
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    second = bind_socket(other)
    docker_strict.validate_board_socket(f"{sock}:{TARGET}")
    with pytest.raises(ValueError, match="sockets"):
        validate_volumes([f"{second}:/run/werkbank/second.sock"])


def test_same_socket_via_docker_volumes_is_still_rejected(run_dir, bind_socket):
    sock = bind_socket(run_dir)
    for source in (sock, run_dir):
        with pytest.raises(ValueError, match="sockets"):
            validate_volumes([f"{source}:{TARGET}"])


def test_parent_directory_is_not_the_socket(run_dir, bind_socket):
    bind_socket(run_dir)
    with pytest.raises(ValueError, match="socket"):
        docker_strict.validate_board_socket(f"{run_dir}:{TARGET}")


def test_regular_file_is_not_a_socket(run_dir):
    fake = run_dir / "board.sock"
    fake.write_text("x", encoding="utf-8")
    os.chmod(fake, 0o600)
    with pytest.raises(ValueError, match="socket"):
        docker_strict.validate_board_socket(f"{fake}:{TARGET}")


def test_symlinked_socket_and_symlinked_directory_are_rejected(run_dir, bind_socket, tmp_path):
    sock = bind_socket(run_dir)
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    (other / "board.sock").symlink_to(sock)
    alias = tmp_path / "alias"
    alias.symlink_to(run_dir, target_is_directory=True)
    for path in (other / "board.sock", alias / "board.sock"):
        with pytest.raises(ValueError, match="symlink"):
            docker_strict.validate_board_socket(f"{path}:{TARGET}")


def test_non_normalized_source_is_rejected(run_dir, bind_socket):
    sock = bind_socket(run_dir)
    for path in (f"{run_dir}/./board.sock", f"{run_dir}/../run/board.sock", f"{run_dir}//board.sock"):
        with pytest.raises(ValueError):
            docker_strict.validate_board_socket(f"{path}:{TARGET}")
    assert docker_strict.validate_board_socket(f"{sock}:{TARGET}")


@pytest.mark.parametrize("sock_mode,dir_mode", [(0o660, 0o700), (0o606, 0o700), (0o600, 0o750), (0o600, 0o755)])
def test_permissions_must_be_private(run_dir, bind_socket, sock_mode, dir_mode):
    sock = bind_socket(run_dir, mode=sock_mode)
    os.chmod(run_dir, dir_mode)
    try:
        with pytest.raises(ValueError, match="private"):
            docker_strict.validate_board_socket(f"{sock}:{TARGET}")
    finally:
        os.chmod(run_dir, 0o700)


def test_foreign_owner_is_rejected(run_dir, bind_socket, monkeypatch):
    sock = bind_socket(run_dir)
    monkeypatch.setattr(docker_strict.os, "getuid", lambda: os.stat(sock).st_uid + 1)
    with pytest.raises(ValueError, match="owned"):
        docker_strict.validate_board_socket(f"{sock}:{TARGET}")


def test_socket_name_is_fixed(run_dir, bind_socket):
    sock = bind_socket(run_dir, name="agent.123")
    with pytest.raises(ValueError, match="board.sock"):
        docker_strict.validate_board_socket(f"{sock}:{TARGET}")


def test_ssh_agent_socket_is_rejected(run_dir, bind_socket, monkeypatch):
    sock = bind_socket(run_dir)
    monkeypatch.setenv("SSH_AUTH_SOCK", str(sock))
    with pytest.raises(ValueError, match="SSH_AUTH_SOCK"):
        docker_strict.validate_board_socket(f"{sock}:{TARGET}")


@pytest.mark.parametrize("target", [
    "/run/werkbank", "/run/werkbank/", "/run/werkbank/sub/board.sock", "/run/werkbank/../docker.sock",
    "/tmp/board.sock", "/run/board.sock", "run/werkbank/board.sock", "/run/werkbank/./board.sock",
    "/run/werkbankx/board.sock",
])
def test_target_is_fixed_under_run_werkbank(run_dir, bind_socket, target):
    sock = bind_socket(run_dir)
    with pytest.raises(ValueError, match="/run/werkbank/"):
        docker_strict.validate_board_socket(f"{sock}:{target}")


def test_other_name_under_run_werkbank_is_allowed(run_dir, bind_socket):
    sock = bind_socket(run_dir)
    assert docker_strict.validate_board_socket(f"{sock}:/run/werkbank/door.sock") == [f"{sock}:/run/werkbank/door.sock:rw"]


def test_mode_suffix_is_rejected(run_dir, bind_socket):
    sock = bind_socket(run_dir)
    with pytest.raises(ValueError):
        docker_strict.validate_board_socket(f"{sock}:{TARGET}:ro")


@pytest.mark.parametrize("path", ["/var/run/docker.sock", "/run/docker.sock", "/run/podman/podman.sock",
                                  "/var/run", "/run", "/run/podman"])
def test_real_docker_socket_paths_and_parents_are_rejected(path):
    with pytest.raises(ValueError, match="Docker socket"):
        docker_strict.validate_board_socket(f"{path}:{TARGET}")


def test_known_docker_socket_and_its_hardlink_are_rejected(run_dir, bind_socket, monkeypatch):
    """A user-owned rootless Docker socket passes every ownership rule; only the sperre stops it."""
    sock = bind_socket(run_dir)
    monkeypatch.setattr(docker_strict, "_docker_socket_paths", lambda: [sock])
    with pytest.raises(ValueError, match="Docker socket"):
        docker_strict.validate_board_socket(f"{sock}:{TARGET}")
    other = run_dir.parent / "linked"
    other.mkdir(mode=0o700)
    os.link(sock, other / "board.sock")
    with pytest.raises(ValueError, match="Docker socket"):
        docker_strict.validate_board_socket(f"{other / 'board.sock'}:{TARGET}")


def test_absent_socket_only_when_not_required(run_dir):
    spec = f"{run_dir / 'board.sock'}:{TARGET}"
    with pytest.raises(ValueError):
        docker_strict.validate_board_socket(spec)
    assert docker_strict.validate_board_socket(spec, require_present=False) == [f"{run_dir / 'board.sock'}:{TARGET}:rw"]


def test_absent_socket_still_needs_private_directory(run_dir):
    os.chmod(run_dir, 0o755)
    try:
        with pytest.raises(ValueError, match="private"):
            docker_strict.validate_board_socket(f"{run_dir / 'board.sock'}:{TARGET}", require_present=False)
    finally:
        os.chmod(run_dir, 0o700)


# --- validate_labels ------------------------------------------------------------------------------

@pytest.mark.parametrize("labels", [None, {}])
def test_no_labels_is_nothing(labels):
    assert docker_strict.validate_labels(labels) == {}


def test_werkbank_labels_pass():
    labels = {"werkbank.sandbox.run": "run-42_a.b", "werkbank.ticket": "WB-1177"}
    assert docker_strict.validate_labels(labels) == labels


@pytest.mark.parametrize("key", [
    "hermes-agent", "hermes-task-id", "com.example.run", "sandbox.run", "werkbank", "werkbank.",
    "werkbankx.run", "Werkbank.run", "werkbank..run", "werkbank.run=x", "werkbank.run ", "",
])
def test_foreign_or_malformed_label_keys_are_rejected(key):
    with pytest.raises(ValueError, match="werkbank."):
        docker_strict.validate_labels({key: "x"})


@pytest.mark.parametrize("value", ["", "a b", "a/b", "x" * 64, "ä", "a=b", 7, None])
def test_label_values_must_already_be_label_safe(value):
    with pytest.raises(ValueError, match="label value"):
        docker_strict.validate_labels({"werkbank.sandbox.run": value})


def test_at_most_four_labels():
    docker_strict.validate_labels({f"werkbank.k{i}": "v" for i in range(4)})
    with pytest.raises(ValueError, match="at most 4"):
        docker_strict.validate_labels({f"werkbank.k{i}": "v" for i in range(5)})


@pytest.mark.parametrize("labels", [["werkbank.sandbox.run=x"], "werkbank.sandbox.run=x"])
def test_labels_must_be_a_mapping(labels):
    with pytest.raises(ValueError, match="mapping"):
        docker_strict.validate_labels(labels)


# --- DockerEnvironment wiring ---------------------------------------------------------------------

def test_board_socket_adds_exactly_one_bind(docker_calls, run_dir, bind_socket):
    sock = bind_socket(run_dir)
    docker.DockerEnvironment("test-image", strict=True)
    before = _flag_values(_run_argv(docker_calls), "-v")
    docker_calls.clear()
    docker.DockerEnvironment("test-image", strict=True, board_socket=f"{sock}:{TARGET}")
    after = _flag_values(_run_argv(docker_calls), "-v")
    assert after == [*before, f"{sock}:{TARGET}:rw"]


def test_board_socket_next_to_volumes(docker_calls, run_dir, bind_socket, tmp_path):
    sock = bind_socket(run_dir)
    work = tmp_path / "work"
    work.mkdir()
    docker.DockerEnvironment("test-image", strict=True, volumes=[f"{work}:/workspace:rw"],
                             board_socket=f"{sock}:{TARGET}")
    assert _flag_values(_run_argv(docker_calls), "-v") == [f"{work}:/workspace:rw", f"{sock}:{TARGET}:rw"]


def test_volume_with_the_run_directory_stays_rejected(docker_calls, run_dir, bind_socket):
    sock = bind_socket(run_dir)
    with pytest.raises(ValueError, match="sockets"):
        docker.DockerEnvironment("test-image", strict=True, volumes=[f"{run_dir}:/workspace"],
                                 board_socket=f"{sock}:{TARGET}")
    assert not docker_calls


def test_invalid_board_socket_never_reaches_docker(docker_calls, run_dir):
    with pytest.raises(ValueError):
        docker.DockerEnvironment("test-image", strict=True, board_socket=f"{run_dir}:{TARGET}")
    assert not docker_calls


def test_labels_land_on_the_container_next_to_hermes_labels(docker_calls):
    docker.DockerEnvironment("test-image", strict=True, task_id="t1",
                             labels={"werkbank.sandbox.run": "run-42"})
    labels = _flag_values(_run_argv(docker_calls), "--label")
    assert "werkbank.sandbox.run=run-42" in labels
    assert "hermes-agent=1" in labels and "hermes-task-id=t1" in labels
    assert labels[-1] == "werkbank.sandbox.run=run-42"


def test_foreign_label_never_reaches_docker(docker_calls):
    with pytest.raises(ValueError, match="werkbank."):
        docker.DockerEnvironment("test-image", strict=True, labels={"hermes-agent": "0"})
    assert not docker_calls


@pytest.mark.parametrize("kwargs", [{"board_socket": "/x/board.sock:/run/werkbank/board.sock"},
                                    {"labels": {"werkbank.sandbox.run": "r"}}])
def test_keys_require_strict(docker_calls, kwargs):
    with pytest.raises(ValueError, match="docker_strict"):
        docker.DockerEnvironment("test-image", persist_across_processes=False, **kwargs)
    assert not docker_calls


# Captured on wb-1090-sandbox-probe (8b653dea69) BEFORE this change: strict `docker run` argv.
STRICT_SNAPSHOT = [
    "docker", "run", "-d", "--init", "--name", "hermes-01234567",
    "--label", "hermes-agent=1", "--label", "hermes-task-id=snap", "--label", "hermes-profile=default",
    "--label", "hermes-egress=strict", "--label", "hermes-environment=68b9ca90fee93f5edde815c2",
    "-w", "/tmp", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
    "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m", "--user", "1234:1234",
    "--cpus", "2", "--memory", "2048m", "--pids-limit", "2048", "--shm-size", "1g", "--network=none",
    "-e", "A", "test-image", "sleep", "infinity"]


def _snapshot_argv(docker_calls, monkeypatch, **kwargs):
    monkeypatch.setattr(docker, "get_hermes_home", lambda: "/snapshot-home")
    monkeypatch.setattr(docker.uuid, "uuid4", lambda: type("U", (), {"hex": "0123456789abcdef" * 2})())
    docker_calls.clear()
    docker.DockerEnvironment("test-image", strict=True, task_id="snap", env={"A": "1"}, network=False,
                             cpu=2, memory=2048, **kwargs)
    return _run_argv(docker_calls)


def test_strict_argv_without_new_keys_is_byte_identical(docker_calls, monkeypatch):
    assert _snapshot_argv(docker_calls, monkeypatch) == STRICT_SNAPSHOT


def test_explicit_empty_keys_equal_omitted(docker_calls, monkeypatch):
    omitted = _snapshot_argv(docker_calls, monkeypatch)
    explicit = _snapshot_argv(docker_calls, monkeypatch, board_socket="", labels={})
    assert explicit == omitted == STRICT_SNAPSHOT


# --- config bridge --------------------------------------------------------------------------------

def test_config_keys_reach_the_strict_container(docker_calls, run_dir, bind_socket):
    import json

    from tools.terminal_scope import reset_terminal_scope, set_terminal_scope
    from tools.terminal_tool import _get_env_config
    from tools.terminal_tool_backends import _build_docker_env, _container_config_from_config
    from hermes_cli.config import TERMINAL_CONFIG_ENV_MAP

    assert TERMINAL_CONFIG_ENV_MAP["docker_board_socket"] == "TERMINAL_DOCKER_BOARD_SOCKET"
    assert TERMINAL_CONFIG_ENV_MAP["docker_labels"] == "TERMINAL_DOCKER_LABELS"
    sock = bind_socket(run_dir)
    token = set_terminal_scope({
        "TERMINAL_ENV": "docker", "TERMINAL_DOCKER_STRICT": "true", "TERMINAL_CONTAINER_DISK": "0",
        "TERMINAL_DOCKER_BOARD_SOCKET": f"{sock}:{TARGET}",
        "TERMINAL_DOCKER_LABELS": json.dumps({"werkbank.sandbox.run": "run-7"})})
    try:
        cfg = _get_env_config()
        assert cfg["docker_board_socket"] == f"{sock}:{TARGET}"
        assert cfg["docker_labels"] == {"werkbank.sandbox.run": "run-7"}
        _build_docker_env(image="test-image", cwd="/tmp", timeout=30,
                          cc=_container_config_from_config(cfg), task_id="cfg", host_cwd=None)
    finally:
        reset_terminal_scope(token)
    argv = _run_argv(docker_calls)
    assert f"{sock}:{TARGET}:rw" in _flag_values(argv, "-v")
    assert "werkbank.sandbox.run=run-7" in _flag_values(argv, "--label")


def test_config_defaults_are_empty():
    from tools.terminal_scope import reset_terminal_scope, set_terminal_scope
    from tools.terminal_tool import _get_env_config

    token = set_terminal_scope({"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_STRICT": "true"})
    try:
        cfg = _get_env_config()
    finally:
        reset_terminal_scope(token)
    assert cfg["docker_board_socket"] == "" and cfg["docker_labels"] == {}

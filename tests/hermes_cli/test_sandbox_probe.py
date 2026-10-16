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


# --- WB-1177: one board socket and werkbank labels ------------------------------------------------

BOARD_TARGET = "/run/werkbank/board.sock"


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A Werkbank-like private run root holding ``board.sock`` (bound relative: sun_path limit)."""
    import socket

    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    sock = socket.socket(socket.AF_UNIX)
    monkeypatch.chdir(root)
    sock.bind("board.sock")
    os.chmod(root / "board.sock", 0o600)
    yield root / "board.sock"
    sock.close()


def test_valid_board_socket_is_reported_and_not_exposed(home, board):
    path = _write(home, dict(STRICT_TERMINAL, docker_board_socket=f"{board}:{BOARD_TARGET}",
                             docker_labels={"werkbank.sandbox.run": "run-1"}))
    rep = _probe(path)
    assert rep["strict"] is True and rep["config_sha256"] == _sha(path)
    assert rep["auth_exposed"] is False, rep["auth_reasons"]
    assert rep["board_socket"] == {"source": str(board), "target": BOARD_TARGET, "present": True}
    assert rep["labels"] == {"werkbank.sandbox.run": "run-1"}


def test_no_board_socket_reports_none(home):
    rep = _probe(_write(home, STRICT_TERMINAL))
    assert rep["board_socket"] is None and rep["labels"] == {}


def test_board_socket_parent_directory_is_exposed(home, board):
    path = _write(home, dict(STRICT_TERMINAL, docker_board_socket=f"{board.parent}:{BOARD_TARGET}"))
    rep = _probe(path)
    assert rep["auth_exposed"] is True
    assert any("docker_board_socket" in r for r in rep["auth_reasons"])


def test_board_socket_symlink_is_exposed(home, board, tmp_path):
    alias = tmp_path / "alias"
    alias.mkdir(mode=0o700)
    (alias / "board.sock").symlink_to(board)
    rep = _probe(_write(home, dict(STRICT_TERMINAL, docker_board_socket=f"{alias / 'board.sock'}:{BOARD_TARGET}")))
    assert rep["auth_exposed"] is True


def test_board_socket_with_open_permissions_is_exposed(home, board):
    os.chmod(board.parent, 0o755)
    rep = _probe(_write(home, dict(STRICT_TERMINAL, docker_board_socket=f"{board}:{BOARD_TARGET}")))
    os.chmod(board.parent, 0o700)
    assert rep["auth_exposed"] is True


def test_board_socket_inside_hermes_home_is_exposed(home, monkeypatch):
    import socket

    inner = home / "run"
    inner.mkdir(mode=0o700)
    os.chmod(inner, 0o700)
    monkeypatch.chdir(inner)
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind("board.sock")
        os.chmod(inner / "board.sock", 0o600)
        rep = _probe(_write(home, dict(STRICT_TERMINAL, docker_board_socket=f"{inner / 'board.sock'}:{BOARD_TARGET}")))
    finally:
        sock.close()
    assert rep["auth_exposed"] is True


def test_board_socket_not_yet_bound_is_reported_absent(home, tmp_path, monkeypatch):
    """The broker may bind after the probe; the container start re-checks the live socket."""
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    root = tmp_path / "later"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    rep = _probe(_write(home, dict(STRICT_TERMINAL, docker_board_socket=f"{root / 'board.sock'}:{BOARD_TARGET}")))
    assert rep["auth_exposed"] is False, rep["auth_reasons"]
    assert rep["board_socket"]["present"] is False


def test_board_socket_without_strict_is_exposed(home, board):
    rep = _probe(_write(home, dict(STRICT_TERMINAL, docker_strict=False,
                                   docker_board_socket=f"{board}:{BOARD_TARGET}")))
    assert rep["auth_exposed"] is True


def test_foreign_label_is_exposed(home):
    rep = _probe(_write(home, dict(STRICT_TERMINAL, docker_labels={"com.example": "x"})))
    assert rep["auth_exposed"] is True
    assert any("docker_labels" in r for r in rep["auth_reasons"])


@pytest.mark.parametrize("var,value", [
    ("TERMINAL_DOCKER_BOARD_SOCKET", "/elsewhere/board.sock:/run/werkbank/board.sock"),
    ("TERMINAL_DOCKER_LABELS", '{"werkbank.sandbox.run": "other"}'),
])
def test_env_setting_a_new_key_breaks_the_hash(home, var, value):
    path = _write(home, STRICT_TERMINAL)
    rep = _probe(path, dict(os.environ, **{var: value}))
    assert var in rep["overrides"]
    assert rep["config_sha256"] is None


def test_new_keys_change_the_config_hash(home, board):
    plain = _sha(_write(home, STRICT_TERMINAL))
    with_socket = _probe(_write(home, dict(STRICT_TERMINAL, docker_board_socket=f"{board}:{BOARD_TARGET}")))
    with_label = _probe(_write(home, dict(STRICT_TERMINAL, docker_labels={"werkbank.sandbox.run": "r"})))
    assert len({plain, with_socket["config_sha256"], with_label["config_sha256"]}) == 3
    assert None not in (with_socket["config_sha256"], with_label["config_sha256"])


def test_non_mapping_labels_are_exposed_not_a_crash(home):
    rep = _probe(_write(home, dict(STRICT_TERMINAL, docker_labels=["werkbank.sandbox.run=x"])))
    assert rep["auth_exposed"] is True and rep["labels"] == {}


# --- WB-855 integration: review findings on WB-1090 ------------------------------------------------

DOCKER_RUNTIME_ENV = {
    "HERMES_DOCKER_BINARY": "/opt/other/docker",
    "DOCKER_HOST": "tcp://10.0.0.9:2375",
    "DOCKER_CONTEXT": "remote",
    "DOCKER_CONFIG": "/tmp/other-docker-config",
}


@pytest.fixture
def no_docker_runtime_env(monkeypatch):
    for var in DOCKER_RUNTIME_ENV:
        monkeypatch.delenv(var, raising=False)


@pytest.mark.parametrize("var", sorted(DOCKER_RUNTIME_ENV))
def test_docker_runtime_env_is_an_override(home, no_docker_runtime_env, var):
    """Which daemon/binary runs the container is part of the policy: another daemon sees other
    host paths, another binary is other code. The file hash cannot vouch for that."""
    path = _write(home, STRICT_TERMINAL)
    rep = _probe(path, dict(os.environ, **{var: DOCKER_RUNTIME_ENV[var]}))
    assert var in rep["overrides"]
    assert rep["config_sha256"] is None and rep["config_matches"] is False


def test_clean_docker_runtime_env_keeps_the_hash(home, no_docker_runtime_env):
    path = _write(home, STRICT_TERMINAL)
    rep = _probe(path)
    assert rep["overrides"] == [] and rep["config_sha256"] == _sha(path)


def test_probe_reports_the_resolved_docker_binary(home, no_docker_runtime_env, monkeypatch):
    from tools.environments import docker
    monkeypatch.setattr(docker, "find_docker", lambda: "/usr/local/bin/docker-x")
    rep = _probe(_write(home, STRICT_TERMINAL))
    assert rep["docker_binary"] == "/usr/local/bin/docker-x"


def _passwd_home():
    import pwd
    return pwd.getpwuid(os.getuid()).pw_dir


@pytest.mark.parametrize("entry", ["", ".ssh", ".ssh/id_ed25519", ".hermes", ".config/gh"])
def test_credential_paths_come_from_passwd_not_HOME(home, tmp_path, monkeypatch, entry):
    """A launcher (or .env) that moves $HOME must not hide the real ~/.ssh or ~/.hermes."""
    fake = tmp_path / "fakehome"
    fake.mkdir()
    monkeypatch.setenv("HOME", str(fake))
    source = os.path.join(_passwd_home(), entry) if entry else _passwd_home()
    rep = _probe(_write(home, dict(STRICT_TERMINAL, docker_volumes=[f"{source}:/workspace:rw"])))
    assert rep["auth_exposed"] is True, (source, rep["auth_reasons"])


@pytest.mark.parametrize("value", [
    "ghp_" + "A1b2C3d4" * 5, "github_pat_" + "A" * 30, "sk-" + "x" * 40,
    "sk-ant-api03-" + "y" * 30, "AKIA" + "ABCDEFGHIJKLMNOP", "xoxb-1234-5678-abcdef",
    "https://user:s3cret@git.example.com/repo.git",
])
def test_docker_env_value_that_looks_like_a_key_is_exposed(home, value):
    """Names are the primary check; known key shapes under a harmless name are caught too."""
    rep = _probe(_write(home, dict(STRICT_TERMINAL, docker_env={"HOME": "/tmp", "GIT_REMOTE": value})))
    assert rep["auth_exposed"] is True
    assert any("GIT_REMOTE" in r for r in rep["auth_reasons"])
    assert not any(value in r for r in rep["auth_reasons"])  # never echo the secret


@pytest.mark.parametrize("value", ["https://git.example.com/repo.git", "/cache/pip", "sk", "skip-tests"])
def test_docker_env_plain_values_are_not_exposed(home, value):
    rep = _probe(_write(home, dict(STRICT_TERMINAL, docker_env={"HOME": "/tmp", "GIT_REMOTE": value})))
    assert rep["auth_exposed"] is False, rep["auth_reasons"]


# --- real process: .env, plugin discovery ---------------------------------------------------------

MARKER_PLUGIN = "wb855-marker"


def _subprocess_env(home):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("TERMINAL_") and k not in DOCKER_RUNTIME_ENV
           and k not in {"HERMES_IGNORE_USER_CONFIG", "PYTHONDONTWRITEBYTECODE"}}
    env["HERMES_HOME"] = str(home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _install_marker_plugin(home):
    """A user plugin that leaves a file the moment its code is imported."""
    import hermes_yaml as yaml

    marker = home / "plugin-code-ran"
    plugin = home / "plugins" / MARKER_PLUGIN
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text(yaml.safe_dump({"name": MARKER_PLUGIN, "version": "0.1.0"}),
                                        encoding="utf-8")
    (plugin / "__init__.py").write_text(
        f"import pathlib\npathlib.Path({str(marker)!r}).write_text('imported')\n"
        "def register(ctx):\n    pass\n", encoding="utf-8")
    return marker


def _run_probe_process(home, *extra):
    import subprocess
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "sandbox-probe", "--config",
         str(home / "config.yaml"), "--json", *extra],
        cwd=repo, env=_subprocess_env(home), capture_output=True, text=True, timeout=120)


def test_process_probe_loads_no_plugin_code(home):
    import subprocess
    from pathlib import Path

    config = dict(STRICT_TERMINAL)
    home.joinpath("config.yaml").write_text(
        json.dumps({"terminal": config, "plugins": {"enabled": [MARKER_PLUGIN]}}), encoding="utf-8")
    marker = _install_marker_plugin(home)
    repo = Path(__file__).resolve().parents[2]
    # Control: the same home under plain plugin discovery DOES run the plugin's code.
    control = subprocess.run(
        [sys.executable, "-c", "from hermes_cli.plugins import discover_plugins; discover_plugins()"],
        cwd=repo, env=_subprocess_env(home), capture_output=True, text=True, timeout=120)
    assert control.returncode == 0, control.stderr
    assert marker.exists(), "control: marker plugin was not loaded; the test proves nothing"
    marker.unlink()

    result = _run_probe_process(home)
    assert result.returncode == 0, result.stderr
    rep = json.loads(result.stdout)
    assert rep["strict"] is True
    assert not marker.exists(), "sandbox-probe imported plugin code"


def test_process_probe_sees_docker_host_from_dotenv(home):
    path = _write(home, STRICT_TERMINAL)
    (home / ".env").write_text("DOCKER_HOST=tcp://10.0.0.9:2375\n", encoding="utf-8")
    result = _run_probe_process(home)
    assert result.returncode == 0, result.stderr
    rep = json.loads(result.stdout)
    assert "DOCKER_HOST" in rep["overrides"]
    assert rep["config_sha256"] is None and rep["config_matches"] is False
    (home / ".env").unlink()
    clean = json.loads(_run_probe_process(home).stdout)
    assert clean["config_sha256"] == _sha(path) and clean["overrides"] == []

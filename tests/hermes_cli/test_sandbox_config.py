"""``hermes chat --sandbox-config FILE``: start a run with the run's own terminal config (WB-1176).

The Werkbank proves strict Docker with ``sandbox-probe`` (WB-1090) and then starts the worker.
Both must use the SAME file through the SAME load/merge code, otherwise the probe certifies a
file the run never reads. Pinned here:

(a) the start path makes the terminal strict Docker with exactly the file's mounts, even when the
    profile says ``terminal.backend: local`` and ``TERMINAL_*`` in the environment say otherwise
    (also after the profile's own config->env bridges run again later);
(b) a file that does not describe strict Docker aborts before any provider/model work;
(c) the probe's ``config_sha256`` for that file equals the hash the start path applied.

Docker is faked (``run_capture``), no model is called, every test runs in a tmp HERMES_HOME.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _strict_terminal(ws: Path, cache: Path) -> dict:
    return {
        "backend": "docker", "docker_strict": True, "docker_image": "python@sha256:" + "a" * 64,
        "cwd": "/workspace", "docker_mount_cwd_to_workspace": False, "docker_run_as_host_user": True,
        "docker_forward_env": [], "docker_env": {"HOME": "/tmp"},
        "docker_volumes": [f"{ws}:/workspace:rw", f"{cache}:/cache:rw"], "docker_network": False,
        "container_cpu": 2, "container_memory": 2048, "container_persistent": False,
        "docker_persist_across_processes": False, "docker_shared_container_key": "",
        "docker_orphan_reaper": False, "container_disk": 0, "docker_extra_args": [],
    }


@pytest.fixture
def run(tmp_path, monkeypatch):
    """Profile home that says LOCAL (file + env), plus a strict run config elsewhere."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in list(os.environ):
        if key.startswith("TERMINAL_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    (home / "config.yaml").write_text(
        "terminal:\n  backend: local\n  docker_strict: false\n"
        "  docker_volumes: ['/srv/profile:/profile']\n", encoding="utf-8")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_DOCKER_STRICT", "false")
    monkeypatch.setenv("TERMINAL_DOCKER_VOLUMES", json.dumps(["/srv/env:/env"]))
    lauf = tmp_path / "lauf"
    (lauf / "workspace").mkdir(parents=True)
    (lauf / "cache").mkdir()
    terminal = _strict_terminal(lauf / "workspace", lauf / "cache")
    cfg = lauf / "config.yaml"
    cfg.write_text(json.dumps({"terminal": terminal}, indent=2), encoding="utf-8")
    import tools.terminal_scope as scope
    import tools.terminal_tool as terminal_tool
    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", False)
    yield {"home": home, "config": cfg, "terminal": terminal}
    import hermes_cli.sandbox_config as sandbox_config
    sandbox_config._reset_for_tests()


@pytest.fixture
def docker_calls(monkeypatch):
    from tools.environments import docker
    calls = []

    def capture(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="null" if "inspect" in argv else "id",
                                           stderr="")
    monkeypatch.setattr(docker, "run_capture", capture)
    monkeypatch.setattr(docker, "find_docker", lambda: "docker")
    monkeypatch.setattr(docker, "_resolve_host_user_spec", lambda: "1234:1234")
    monkeypatch.setattr(docker, "_cgroup_limits_ok", True)
    monkeypatch.setattr(docker.DockerEnvironment, "init_session", lambda self: None)
    return calls


def _parse(argv):
    from hermes_cli.main import _build_cli_parser
    return _build_cli_parser()[0].parse_args(argv)


def _no_model_work(monkeypatch, cli_main):
    """Everything in cmd_chat that could reach a provider is replaced; ``cli.main`` is *cli_main*."""
    import cli
    import hermes_cli.free_tier_bootstrap as bootstrap
    import hermes_cli.main as main
    import hermes_cli.observability.shared_metrics_consent as consent
    import hermes_cli.observability.shared_metrics_process as metrics
    monkeypatch.setattr(cli, "main", cli_main)
    monkeypatch.setattr(bootstrap, "run_bootstrap", lambda **kw: None)
    monkeypatch.setattr(main, "_has_any_provider_configured", lambda **kw: True)
    monkeypatch.setattr(main, "_start_chat_background_prefetch", lambda: None)
    monkeypatch.setattr(main, "_warn_retired_xai_models", lambda: None)
    monkeypatch.setattr(main, "_confirm_startup_expensive_model_override", lambda args: None)
    monkeypatch.setattr(consent, "offer_consent_before_chat", lambda args: None)
    monkeypatch.setattr(metrics, "begin_process", lambda *a, **k: None)
    return main


def _start(monkeypatch, config, seen, docker_calls=None):
    """``hermes chat --sandbox-config F --oneshot -q hi``; the fake cli.main records what the run sees."""
    def fake_cli_main(**kwargs):
        # The profile's own launch bridges run AFTER the pin in a real chat (cli.py env_mappings,
        # env_loader reload): they must not win.
        from hermes_cli.config import apply_terminal_config_to_env
        apply_terminal_config_to_env(env=None, override=True)
        os.environ["TERMINAL_ENV"] = "local"
        os.environ["TERMINAL_DOCKER_STRICT"] = "false"
        from tools.terminal_tool import _get_env_config
        from tools.terminal_tool_backends import _build_docker_env, _container_config_from_config
        from tools.docker_strict import tool_denial
        cfg = _get_env_config()
        seen["cfg"] = cfg
        seen["kwargs"] = kwargs
        seen["denial_execute_code"] = tool_denial("execute_code")
        import hermes_cli.sandbox_config as sandbox_config
        seen["applied"] = sandbox_config.applied_sandbox_config()
        if docker_calls is not None and cfg["env_type"] == "docker":
            env = _build_docker_env(image=cfg["docker_image"], cwd=cfg["cwd"], timeout=30,
                                    cc=_container_config_from_config(cfg), task_id="wb1176",
                                    host_cwd=cfg.get("host_cwd"))
            seen["env_strict"] = env._strict
            argv = next(a for a in docker_calls if "run" in a)
            seen["mounts"] = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-v"]
            seen["network_none"] = "--network=none" in argv
    main = _no_model_work(monkeypatch, fake_cli_main)
    main.cmd_chat(_parse(["chat", "--sandbox-config", str(config), "--oneshot", "-q", "hi"]))


# (a) -------------------------------------------------------------------------------------------

def test_chat_sandbox_config_runs_strict_docker_with_the_files_mounts(run, monkeypatch, docker_calls):
    seen: dict = {}
    _start(monkeypatch, run["config"], seen, docker_calls)
    cfg = seen["cfg"]
    assert cfg["env_type"] == "docker"
    assert cfg["docker_strict"] is True
    assert cfg["docker_volumes"] == run["terminal"]["docker_volumes"]
    assert cfg["docker_network"] is False
    assert seen["env_strict"] is True
    assert seen["mounts"] == run["terminal"]["docker_volumes"]
    assert seen["network_none"] is True
    assert seen["denial_execute_code"]  # strict tool policy really governs the run
    assert seen["kwargs"]["query"] == "hi" and seen["kwargs"]["oneshot"] is True


def test_chat_sandbox_config_ignores_profile_scope_and_env_for_terminal(run, monkeypatch):
    """A later terminal scope / env write cannot widen the pinned policy."""
    seen: dict = {}
    _start(monkeypatch, run["config"], seen)
    from tools.terminal_scope import set_terminal_scope, reset_terminal_scope, terminal_env
    token = set_terminal_scope({"TERMINAL_ENV": "local", "TERMINAL_DOCKER_STRICT": "false"})
    try:
        assert terminal_env("TERMINAL_ENV") == "docker"
        assert terminal_env("TERMINAL_DOCKER_STRICT") == "true"
    finally:
        reset_terminal_scope(token)
    os.environ["TERMINAL_DOCKER_VOLUMES"] = json.dumps(["/:/host"])
    assert json.loads(terminal_env("TERMINAL_DOCKER_VOLUMES")) == run["terminal"]["docker_volumes"]


# (b) -------------------------------------------------------------------------------------------

def _bad_files(run):
    t = dict(run["terminal"])
    yield "strict_off", {"terminal": {**t, "docker_strict": False}}
    yield "strict_missing", {"terminal": {k: v for k, v in t.items() if k != "docker_strict"}}
    yield "strict_string", {"terminal": {**t, "docker_strict": "yes"}}
    yield "backend_local", {"terminal": {**t, "backend": "local"}}
    yield "no_terminal", {}
    yield "extra_top_level", {"terminal": t, "model": {"default": "x"}}
    yield "unknown_terminal_key", {"terminal": {**t, "docker_privileged": True}}
    yield "extra_args", {"terminal": {**t, "docker_extra_args": ["--privileged"]}}
    yield "socket_volume", {"terminal": {**t, "docker_volumes": ["/var/run:/var/run"]}}


@pytest.mark.parametrize("case", ["strict_off", "strict_missing", "strict_string", "backend_local",
                                  "no_terminal", "extra_top_level", "unknown_terminal_key",
                                  "extra_args", "socket_volume", "missing_file", "not_yaml"])
def test_chat_refuses_a_non_strict_run_config_before_any_model_work(run, monkeypatch, capsys, case):
    path = run["config"]
    if case == "missing_file":
        path = path.with_name("nope.yaml")
    elif case == "not_yaml":
        path.write_text("terminal: [unclosed\n", encoding="utf-8")
    else:
        path.write_text(json.dumps(dict(_bad_files(run))[case]), encoding="utf-8")

    def forbidden(*a, **k):
        pytest.fail("model/provider work started despite a non-strict run config")
    main = _no_model_work(monkeypatch, forbidden)
    import hermes_cli.free_tier_bootstrap as bootstrap
    monkeypatch.setattr(bootstrap, "run_bootstrap", forbidden)
    monkeypatch.setattr(main, "_has_any_provider_configured", forbidden)
    with pytest.raises(SystemExit) as exc:
        main.cmd_chat(_parse(["chat", "--sandbox-config", str(path), "--oneshot", "-q", "hi"]))
    assert exc.value.code == 2
    assert "--sandbox-config" in capsys.readouterr().err
    import tools.terminal_scope as scope
    assert scope.get_process_terminal_pin() is None


@pytest.mark.parametrize("flag", [["--tui"], ["--worktree"]])
def test_chat_sandbox_config_refuses_paths_that_leave_this_process(run, monkeypatch, flag):
    def forbidden(*a, **k):
        pytest.fail("started")
    main = _no_model_work(monkeypatch, forbidden)
    with pytest.raises(SystemExit) as exc:
        main.cmd_chat(_parse(["chat", "--sandbox-config", str(run["config"]), *flag, "-q", "hi"]))
    assert exc.value.code == 2


# (c) -------------------------------------------------------------------------------------------

def _probe_json(argv):
    from hermes_cli.sandbox_probe import cmd_sandbox_probe
    import hermes_cli.main as main
    args = _parse(["sandbox-probe", *argv, "--json"])
    assert args.func is main.cmd_sandbox_probe
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_sandbox_probe(args)
    return rc, json.loads(out.getvalue())


def test_probe_hash_equals_the_hash_the_start_path_applies(run, monkeypatch):
    rc, report = _probe_json(["--sandbox-config", str(run["config"])])
    assert rc == 0, report
    import tools.terminal_scope as scope
    assert scope.get_process_terminal_pin() is None  # the probe pins only for its own reading

    seen: dict = {}
    _start(monkeypatch, run["config"], seen)
    want = hashlib.sha256(run["config"].read_bytes()).hexdigest()
    assert seen["applied"].sha256 == want
    assert report["config_sha256"] == seen["applied"].sha256
    assert report["strict"] is True and report["docker_network"] is False
    assert report["auth_exposed"] is False, report["auth_reasons"]
    assert report["overrides"] == [] and report["config_matches"] is True
    assert report["config_path"] == str(run["config"])
    assert report["mode"] == "sandbox-config"


def test_probe_refuses_a_run_config_the_start_path_would_refuse(run):
    run["config"].write_text(json.dumps({"terminal": {**run["terminal"], "docker_strict": False}}),
                             encoding="utf-8")
    rc, report = _probe_json(["--sandbox-config", str(run["config"])])
    assert rc == 2 and "error" in report


def test_probe_and_start_share_one_loader(run, monkeypatch):
    """Same function object on both paths: a change to loading/merging changes both."""
    import hermes_cli.sandbox_config as sandbox_config
    calls = []
    real = sandbox_config.load_sandbox_config

    def spy(path):
        calls.append(str(path))
        return real(path)
    monkeypatch.setattr(sandbox_config, "load_sandbox_config", spy)
    _probe_json(["--sandbox-config", str(run["config"])])
    _start(monkeypatch, run["config"], {})
    assert calls == [str(run["config"]), str(run["config"])]


# without the switch ----------------------------------------------------------------------------

def test_without_switch_chat_does_not_pin_and_passes_the_same_kwargs(run, monkeypatch):
    seen = {}

    def fake_cli_main(**kwargs):
        import tools.terminal_scope as scope
        seen["pin"] = scope.get_process_terminal_pin()
        seen["kwargs"] = kwargs
    main = _no_model_work(monkeypatch, fake_cli_main)
    args = _parse(["chat", "--oneshot", "-q", "hi"])
    assert args.sandbox_config is None
    main.cmd_chat(args)
    assert seen["pin"] is None
    assert "sandbox_config" not in seen["kwargs"]
    assert os.environ["TERMINAL_ENV"] == "local"


def test_probe_config_mode_requires_exactly_one_source():
    with pytest.raises(SystemExit):
        _parse(["sandbox-probe", "--json"])
    with pytest.raises(SystemExit):
        _parse(["sandbox-probe", "--config", "a", "--sandbox-config", "b"])


def test_a_strict_profile_with_other_mounts_still_loses_to_the_run_file(run, monkeypatch, docker_calls,
                                                                      tmp_path):
    """Profile AND env describe valid strict Docker, but with other mounts: the file still wins,
    and the probe's hash is the file's (a swapped merge passes validation here and must still fail)."""
    other = tmp_path / "profile_mount"
    other.mkdir()
    (run["home"] / "config.yaml").write_text(json.dumps({"terminal": {
        "backend": "docker", "docker_strict": True, "docker_network": True,
        "docker_volumes": [f"{other}:/other:rw"], "container_disk": 0}}), encoding="utf-8")
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_DOCKER_STRICT", "true")
    monkeypatch.setenv("TERMINAL_DOCKER_NETWORK", "true")
    monkeypatch.setenv("TERMINAL_DOCKER_VOLUMES", json.dumps([f"{other}:/env:rw"]))
    rc, report = _probe_json(["--sandbox-config", str(run["config"])])
    assert rc == 0, report
    assert report["overrides"] == []
    assert report["config_sha256"] == hashlib.sha256(run["config"].read_bytes()).hexdigest()
    assert report["docker_network"] is False

    seen: dict = {}
    _start(monkeypatch, run["config"], seen, docker_calls)
    assert seen["mounts"] == run["terminal"]["docker_volumes"]
    assert seen["cfg"]["docker_network"] is False and seen["network_none"] is True
    assert report["config_sha256"] == seen["applied"].sha256


# WB-855 integration: board socket + labels go through the same loader (WB-1177 x WB-1176) -------

BOARD_TARGET = "/run/werkbank/board.sock"


@pytest.fixture
def board_dir(tmp_path, monkeypatch):
    """A private run root where the broker will bind ``board.sock`` (not bound yet)."""
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    root = tmp_path / "wbrun"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    return root


def _with(run, **extra):
    run["config"].write_text(json.dumps({"terminal": {**run["terminal"], **extra}}), encoding="utf-8")


@pytest.mark.parametrize("bad", [
    {"docker_board_socket": "PARENT:" + BOARD_TARGET},            # a directory, not the socket
    {"docker_board_socket": "SOCK:/run/elsewhere/board.sock"},     # target outside /run/werkbank
    {"docker_labels": {"com.example.owner": "x"}},                # foreign label namespace
    {"docker_labels": {"werkbank.run": "has space"}},              # value would be rewritten
    {"docker_labels": ["werkbank.run=x"]},                         # not a mapping
])
def test_run_config_with_invalid_socket_or_labels_is_refused(run, board_dir, bad):
    from hermes_cli import sandbox_config

    value = {k: (v.replace("PARENT", str(board_dir)).replace("SOCK", str(board_dir / "board.sock"))
                 if isinstance(v, str) else v) for k, v in bad.items()}
    _with(run, **value)
    with pytest.raises(sandbox_config.SandboxConfigError):
        sandbox_config.load_sandbox_config(run["config"])
    rc, report = _probe_json(["--sandbox-config", str(run["config"])])
    assert rc == 2 and "error" in report


def test_open_board_socket_directory_is_refused(run, board_dir):
    from hermes_cli import sandbox_config

    os.chmod(board_dir, 0o755)
    _with(run, docker_board_socket=f"{board_dir / 'board.sock'}:{BOARD_TARGET}")
    with pytest.raises(sandbox_config.SandboxConfigError):
        sandbox_config.load_sandbox_config(run["config"])


def test_sandbox_config_probe_reports_board_socket_and_labels(run, board_dir):
    labels = {"werkbank.sandbox.run": "run-42", "werkbank.ticket": "wb-855"}
    _with(run, docker_board_socket=f"{board_dir / 'board.sock'}:{BOARD_TARGET}", docker_labels=labels)
    rc, report = _probe_json(["--sandbox-config", str(run["config"])])
    assert rc == 0, report
    assert report["board_socket"] == {"source": str(board_dir / "board.sock"), "target": BOARD_TARGET,
                                      "present": False}
    assert report["labels"] == labels
    assert report["auth_exposed"] is False, report["auth_reasons"]
    assert report["config_sha256"] == hashlib.sha256(run["config"].read_bytes()).hexdigest()


def test_sandbox_config_probe_without_socket_reports_none(run):
    rc, report = _probe_json(["--sandbox-config", str(run["config"])])
    assert rc == 0 and report["board_socket"] is None and report["labels"] == {}


@pytest.mark.parametrize("var,value", [("DOCKER_HOST", "tcp://10.0.0.9:2375"),
                                       ("DOCKER_CONTEXT", "remote"),
                                       ("HERMES_DOCKER_BINARY", "/opt/other/docker")])
def test_sandbox_config_probe_counts_docker_runtime_env_as_override(run, monkeypatch, var, value):
    monkeypatch.setenv(var, value)
    rc, report = _probe_json(["--sandbox-config", str(run["config"])])
    assert rc == 0
    assert var in report["overrides"]
    assert report["config_sha256"] is None and report["config_matches"] is False


# WB-855 review: the pin must survive ``import cli`` and reach every backend reader ---------------

_CHILD = r"""
import json, os, subprocess, sys
from hermes_cli import sandbox_config
sandbox_config.apply_sandbox_config(sandbox_config.load_sandbox_config(sys.argv[1]))
import cli  # load_cli_config -> _mirror_config_to_env runs here with the PROFILE's terminal block
names = ("TERMINAL_ENV", "TERMINAL_TIMEOUT", "TERMINAL_DOCKER_STRICT")
grand = subprocess.run([sys.executable, "-c",
                        "import json, os, sys; print(json.dumps({n: os.environ.get(n) for n in sys.argv[1:]}))",
                        *names], capture_output=True, text=True, check=True)
print("WB1176:" + json.dumps({"self": {n: os.environ.get(n) for n in names},
                              "child": json.loads(grand.stdout.strip().splitlines()[-1])}))
"""


def test_import_cli_after_the_pin_keeps_the_run_policy_in_environ(tmp_path):
    """Repro: profile local/timeout 999, run file strict docker/timeout 60 -> after ``import cli``
    os.environ (and every subprocess) still carries the run's values, not the profile's."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("terminal:\n  backend: local\n  timeout: 999\n", encoding="utf-8")
    lauf = tmp_path / "lauf"
    (lauf / "workspace").mkdir(parents=True)
    (lauf / "cache").mkdir()
    cfg = lauf / "config.yaml"
    cfg.write_text(json.dumps({"terminal": {**_strict_terminal(lauf / "workspace", lauf / "cache"),
                                            "timeout": 60}}), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("TERMINAL_")}
    env.update(HERMES_HOME=str(home), PYTHONPATH=str(REPO), TERMINAL_ENV="local", TERMINAL_TIMEOUT="999")
    env.pop("HERMES_IGNORE_USER_CONFIG", None)
    proc = subprocess.run([sys.executable, "-c", _CHILD, str(cfg)], cwd=str(tmp_path), env=env,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    line = next(l for l in proc.stdout.splitlines() if l.startswith("WB1176:"))
    seen = json.loads(line[len("WB1176:"):])
    want = {"TERMINAL_ENV": "docker", "TERMINAL_TIMEOUT": "60", "TERMINAL_DOCKER_STRICT": "true"}
    assert seen["self"] == want
    assert seen["child"] == want


def test_backend_readers_follow_the_pin_not_profile_or_environ(run, monkeypatch):
    """subagent_worktree.local_backend_active and run_agent._launch_cwd_for_session read the
    backend through the pin: a docker run never stamps a host cwd or offers host worktrees."""
    from hermes_cli import sandbox_config
    from run_agent import _launch_cwd_for_session
    from tools import subagent_worktree

    assert subagent_worktree.local_backend_active() is True  # profile says local
    assert _launch_cwd_for_session("cli") is not None
    sandbox_config.apply_sandbox_config(sandbox_config.load_sandbox_config(run["config"]))
    os.environ["TERMINAL_ENV"] = "local"  # a stale ambient write must not matter
    assert subagent_worktree.local_backend_active() is False
    assert _launch_cwd_for_session("cli") is None


def test_sandbox_config_probe_reports_start_path_environ_drift(run, monkeypatch):
    """The probe compares os.environ of the REAL start path (pin + ``import cli``) with the policy."""
    import hermes_cli.sandbox_probe as sandbox_probe

    rc, report = _probe_json(["--sandbox-config", str(run["config"])])
    assert rc == 0 and report["overrides"] == [] and report["config_matches"] is True

    def drifted(path):
        from hermes_cli import sandbox_config
        policy = dict(sandbox_config.load_sandbox_config(path).policy)
        policy["TERMINAL_ENV"] = "local"
        return policy
    monkeypatch.setattr(sandbox_probe, "_start_path_terminal_environ", drifted)
    rc, report = _probe_json(["--sandbox-config", str(run["config"])])
    assert rc == 0 and "TERMINAL_ENV" in report["overrides"]
    assert report["config_sha256"] is None and report["config_matches"] is False

    monkeypatch.setattr(sandbox_probe, "_start_path_terminal_environ", lambda path: None)
    rc, report = _probe_json(["--sandbox-config", str(run["config"])])
    assert rc == 0 and "start_path_unverified" in report["overrides"]
    assert report["config_sha256"] is None

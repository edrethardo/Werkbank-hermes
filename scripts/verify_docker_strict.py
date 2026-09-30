"""Real WB-855 container acceptance probe; no credentials are read on the host.

Run from the Hermes checkout with its Python, an existing trusted Python/Git
image, and a fresh scratch directory. Output is assertion-backed JSON. All
containers are removed in finally; no host config or profile is modified.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.environments.docker import DockerEnvironment
from tools.terminal_scope import set_terminal_scope, reset_terminal_scope
from tools import terminal_tool as terminal
from tools.file_tools import read_file_tool, clear_file_ops_cache


def verify(image, root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    workspace = root / 'workspace'
    workspace.mkdir()
    (workspace / 'visible.txt').write_text('CONTAINER-READ-WORKS\n')
    canary = root / 'unmounted-canary'
    canary.write_text('HOST-CANARY-MUST-NOT-LEAK\n')
    scope = set_terminal_scope({'TERMINAL_ENV': 'docker', 'TERMINAL_DOCKER_STRICT': 'true'})
    env = None
    task_id = 'wb855-strict-probe'
    try:
        env = DockerEnvironment(image, strict=True, cwd='/workspace', cpu=1, memory=256,
            disk=0, network=False, volumes=[f'{workspace}:/workspace:rw'],
            env={'HOME': '/tmp', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'},
            task_id=task_id, shm_size='64m')
        script = '''import json, os, pathlib
paths = ['/data/projects/agent_ticket/session-secret',
'/data/projects/agent_ticket/config.json', '/data/projects/agent_ticket/state.json',
'/data/projects/agent_ticket/todos.json', '/home/aaron/.hermes/.env',
'/home/aaron/.hermes/auth.json', '/home/aaron/.ssh',
'/home/aaron/.local/state/werkbank/session-secret',
'/home/aaron/.hermes/profiles/werkbank/.env',
'/home/aaron/.hermes/profiles/werkbank/auth.json', '/root/.hermes/.env',
'/root/.hermes/auth.json', '/root/.ssh']
results = {}
for path in paths:
    try:
        if path.endswith('/.ssh'):
            with os.scandir(path) as entries: next(entries, None)
        else:
            with open(path, 'rb') as f: f.read(1)
        results[path] = 'UNEXPECTEDLY_READABLE'
    except OSError as e:
        results[path] = type(e).__name__
assert 'UNEXPECTEDLY_READABLE' not in results.values(), results
assert not pathlib.Path('/var/run/docker.sock').exists()
limits = {name: pathlib.Path('/sys/fs/cgroup/'+name).read_text().strip()
          for name in ['memory.max', 'cpu.max', 'pids.max']}
assert limits['memory.max'] != 'max'
assert not limits['cpu.max'].startswith('max')
assert limits['pids.max'] != 'max'
assert os.getuid() != 0
print(json.dumps({'attempts': results, 'docker_socket': 'absent', 'uid': os.getuid(), 'limits': limits}))
'''
        result = env.execute('python3 -c ' + shlex.quote(script), timeout=30)
        assert result['returncode'] == 0, result
        report = json.loads(result['output'])
        git = env.execute('git init -q && git -c user.name=Probe -c user.email=probe@example.invalid add visible.txt && git -c user.name=Probe -c user.email=probe@example.invalid commit -qm probe && git -c http.connectTimeout=2 -c http.lowSpeedTime=2 -c http.lowSpeedLimit=1 push --no-verify https://192.0.2.1/denied.git HEAD:refs/heads/probe', timeout=15)
        assert git['returncode'] != 0 and 'Failed to connect to 192.0.2.1' in git['output'], git
        report['git_push'] = {'returncode': git['returncode'], 'output': git['output']}
        # Real read_file dispatch uses the strict DockerEnvironment; its host
        # path helper must never run (even the existence of canary is secret).
        with terminal._env_lock:
            terminal._active_environments[task_id] = env
        visible = json.loads(read_file_tool('/workspace/visible.txt', task_id=task_id))
        assert 'CONTAINER-READ-WORKS' in visible.get('content', ''), visible
        hidden = json.loads(read_file_tool(str(canary), task_id=task_id))
        assert 'HOST-CANARY-MUST-NOT-LEAK' not in json.dumps(hidden), hidden
        assert hidden.get('error'), hidden
        report['read_file'] = {'workspace': 'readable', 'host_canary': hidden['error']}
        assert env._container_id is not None
        inspection = subprocess.run(['docker', 'inspect', env._container_id], check=True,
                                    capture_output=True, text=True)
        settings = json.loads(inspection.stdout)[0]
        report['network'] = settings['HostConfig']['NetworkMode']
        assert report['network'] == 'none'
        report['mount_sources'] = [m['Source'] for m in settings['Mounts'] if m['Type'] == 'bind']
        assert report['mount_sources'] == [str(workspace)]
        return report
    finally:
        with terminal._env_lock:
            terminal._active_environments.pop(task_id, None)
        clear_file_ops_cache(task_id)
        if env is not None:
            env.cleanup(force_remove=True)
            assert env.wait_for_cleanup(60), 'strict probe cleanup timed out'
        reset_terminal_scope(scope)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', required=True)
    parser.add_argument('--work-root', required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.image, args.work_root), indent=2))

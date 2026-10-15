---
title: "Strict Docker tool execution"
description: "Opt-in, fail-closed Docker terminal policy and its trust boundaries"
---

# Strict Docker tool execution

`terminal.docker_strict` defaults to **false**. Existing Docker behavior is
unchanged until it is enabled. Enable it in a **new session**, not halfway through
an existing conversation or by modifying the environment from an agent command.

```yaml
terminal:
  backend: docker
  docker_strict: true
  docker_image: python:3.11-slim  # operator must pull/verify the image first
  cwd: /tmp                     # writable container path, not a host cwd
  docker_network: false         # recommended; strict alone is not a firewall
  container_cpu: 1
  container_memory: 512
  container_disk: 0              # disk quotas remain storage-driver-dependent
  docker_volumes: []             # or explicit /absolute/source:/workspace:ro
  docker_env: {}                 # explicit values only
  docker_forward_env: []         # explicit names only; values remain profile-scoped
  docker_extra_args: []          # strict permits no extra Docker arguments
```

## Guarantees at the model-tool boundary

- Only **foreground `terminal` and container-only `read_file`** are admitted.
  `read_file` takes a dedicated text-only path through the actual strict Docker
  environment BEFORE host path resolution, metadata, document extraction or
  follow-up hooks. A visible container file is readable; a host-only canary is
  not. Document conversion is intentionally unavailable in this path.
  The catalog is narrowed and dispatch rejects all other tools, including new
  tools, file writes/search/patch, `execute_code`, browser, web, vision, process
  management, delegation, MCP, connectors and deferred wrappers. Checks cover
  direct registry and sequential/concurrent agent-inline dispatch. Use shell
  commands (including Python) **inside the terminal container** for those file
  operations. Background/PTY/host-local execution and automatic
  foreground-to-background promotion are refused.
- Container commands must use an actual strict `DockerEnvironment`; a cached
  local or non-strict environment is refused rather than reused. A non-Docker
  backend with strict enabled is an error, never a host fallback.
- No automatic host credential, skills, cache, cwd, workspace or home binds.
  Persistence and cross-process container reuse are disabled. Only configured
  `docker_volumes` are bound. Docker's own system mounts and a bounded `/tmp`
  tmpfs still exist; these are not host credential/workspace mounts.
- Only existing absolute bind sources are supported. Named volumes, ambiguous
  syntax, propagation options, system filesystem targets, sockets and special
  files are refused. Sources are canonicalized and directory descendants are
  checked, including symlinks. Known Docker/Podman socket paths and their parent
  directories are rejected, even when the socket is absent. An unreadable or
  broken source fails closed. Images declaring Docker `VOLUME`s are refused to
  prevent automatic anonymous volumes; an uninspectable/unpulled image fails.
- `docker_run_as_host_user` is forced on. Missing POSIX IDs and UID 0 are refused.
  All capabilities are dropped, `no-new-privileges` is mandatory, and snap
  compatibility is forbidden. Root-requiring image entrypoints are unsupported.
- Failure of the cgroup CPU/memory/PID probe prevents workload creation. Positive
  CPU and memory limits are required (zero uses 1 CPU / 5120 MiB); PID limit is
  256. The strict probe itself has no host binds, runs non-root without network
  or capabilities, overrides the image entrypoint, and removes its named
  disposable container, including after a timeout.
- Automatic `.env`, skill passthrough and egress-proxy credential injection are
  disabled. `docker_env` and explicitly named `docker_forward_env` are the only
  host environment values deliberately injected. Image-defined environment and
  container shell startup files still belong to the trusted image.

## Not a complete sandbox for the Hermes controller

This policy confines **model tool execution**, not the entire Hermes process.
The controller still runs on the host with its provider credentials, configuration,
logs and session state. Trusted plugins/middleware, prompt/context assembly,
attachments/media delivery, user slash commands and other control-plane features
are outside this tool-dispatch boundary. In particular, this change does **not**
certify that arbitrary model-generated media paths cannot reach a host-side
attachment resolver. Do not expose this as a complete hostile-user sandbox on a
credential-bearing controller without separately auditing/confining those paths.

The operator, Docker daemon and image publisher remain trusted. Explicit writable
binds grant writes to those host files: never bind controller source, configuration,
credentials or other sensitive paths. Directory scans are preflight checks, not
an atomic filesystem security boundary: trusted host processes must not replace
sources or add sockets after validation. A writable bind can intentionally share
files with host consumers; those consumers must not execute untrusted outputs.

Network access follows `docker_network` (legacy default: true). With networking
enabled, reachable host/LAN services, including an insecure TCP Docker daemon,
remain reachable. Prefer `docker_network: false` or a separately enforced network
policy. Disk quota support remains best-effort; strict mode does not promise a
hard disk quota, a VM boundary, or protection against container/kernel exploits.

## Development verification and probe API

The low-level API is `DockerEnvironment(image=..., strict=True, cwd="/tmp",
network=False, volumes=[...], cpu=1, memory=512, disk=0)`. Check
`env._container_id` with Docker inspect, call `env.execute(command)`, and always
finish with `env.cleanup(force_remove=True); env.wait_for_cleanup(60)`.

For real configuration/dispatch tests, write a disposable profile's `config.yaml`,
build it using `tools.terminal_scope.build_profile_terminal_scope(home)`, bind with
`set_terminal_scope(mapping)`, and restore the token with `reset_terminal_scope`.
Call `model_tools.handle_function_call(name, args, task_id=unique_id)`.
No production profile edits or gateway restart are required.

Run tests with `scripts/run_tests.sh`, never bare pytest. The optional runner knob
`HERMES_TEST_SCRATCH_ROOT=/absolute/scratch` keeps per-file test roots in an
operator-approved directory (default remains the existing disk-backed root).
Long scratch paths require tests using Unix sockets to bind relative to their
fixture directory.

WB855 development record: new tests cover strict mount/env/resource flags,
fail-closed cgroups, socket aliases and parents, all extra arguments being denied,
non-root identity, implicit image volumes, exhaustive registry denial, direct
file/code and agent-inline dispatch, cached-host refusal, real profile YAML A→B→A,
and backend configuration wiring. Existing Docker environment/cgroup/network/
session-isolation/config-bridge tests are run alongside them. The parent task
performs independent real-container acceptance; mocked constructor tests alone
are not proof of live Docker isolation.

## One board socket and run labels (`docker_board_socket`, `docker_labels`)

`docker_volumes` rejects every Unix socket, including sockets inside a bound directory. A
launcher that hands the container exactly one door back to its board uses a separate key:

```yaml
terminal:
  docker_strict: true
  docker_board_socket: "/abs/run-root/board.sock:/run/werkbank/board.sock"
  docker_labels: {"werkbank.sandbox.run": "<run-id>"}
```

`docker_board_socket` accepts exactly one `source:target` pair (no mode suffix; the bind is
`rw` because connecting needs write). The source must be the socket itself: an absolute,
normalized path with no symlink anywhere, named `board.sock`, owned by the calling user with no
group/other permission bits, inside a caller-owned `0700` directory. Known Docker/Podman socket
paths, their parents, a hard link to one of them and `$SSH_AUTH_SOCK` are rejected. The target
must be a file directly under `/run/werkbank/`. The socket is re-checked at container start.

`docker_labels` adds at most four labels whose keys start with `werkbank.`; values must already
be label-safe (`[A-Za-z0-9_.-]`, 1–63 characters) and are rejected instead of rewritten. They are
added after the Hermes labels, which they cannot replace.

Both keys require `docker_strict: true`; without them the `docker run` arguments are unchanged.
`sandbox-probe` reports them as `board_socket` and `labels` and counts an invalid socket or label
as `auth_exposed`.

## Proving the effective policy to a launcher (`sandbox-probe`, `sessions reserve`)

A config file a launcher wrote proves nothing by itself: a profile `.env`, the ambient
environment, a managed-scope overlay or `--ignore-user-config` can change what Hermes
actually runs with. Ask the binary instead:

```bash
hermes -p <profile> sandbox-probe --config <run-config.yaml> --json
```

It resolves the terminal policy through the same config-to-env bridge a run of that profile
uses (on a copy of the environment) and the terminal tool's own parser, then prints one JSON
object. Read the fields, not the exit code (non-zero only means nothing was provable):

| Field | Meaning |
|---|---|
| `strict` | `true` only when `docker_strict` is on and the backend is Docker |
| `config_sha256` | sha256 of the profile's `config.yaml`; `null` when no file governs the run or `overrides` is non-empty |
| `docker_network` | `false` only for a Docker backend with `docker_network: false` |
| `auth_exposed` | `false` only when strict and nothing forwards credentials (no `docker_forward_env`, no credential-named `docker_env`, no extra args, no bind of the Hermes home or a host credential directory); see `auth_reasons` |
| `config_matches` | the `--config` file's bytes equal the effective `config.yaml` |
| `overrides` | `TERMINAL_*` values where env/.env/managed scope differ from file plus defaults |
| `board_socket` | the one board socket (`source`, `target`, `present`) or `null` |
| `labels` | the `werkbank.*` container labels |

The probe is read-only: it starts no container, opens no network and does not touch the
session store. It does not prove Docker itself is healthy; a launch still fails closed when
the container cannot be created.

A trusted launcher that must bind a session id before the run starts reserves one:

```bash
hermes -p <profile> sessions reserve --json
# {"session_id": "20261015_212739_6e2990", "profile": "<profile>", "state_db": ".../state.db"}
```

This inserts exactly one new, empty session row through the session store's own write path
and never adopts an existing id. Start that session afterwards with `--resume <id>`.

## Starting a run with its own config (`chat --sandbox-config`)

`--config` above checks the profile's own `config.yaml`. A launcher that writes a per-run
file instead starts the worker with it and probes exactly that path:

```bash
hermes -p <profile> sandbox-probe --sandbox-config <run>/config.yaml --json
hermes -p <profile> chat --resume <session> --sandbox-config <run>/config.yaml --oneshot -q "<prompt>"
```

Both go through the same loader. The file may contain only a `terminal:` section with known
`terminal.*` keys, must say `backend: docker` and `docker_strict: true`, and must pass the
strict volume/extra-args checks; otherwise `chat` exits 2 before any provider or model work
and the probe exits 2. The file's section plus shipped defaults becomes the complete terminal
policy of the process: the profile's `terminal.*`, its `.env` and `TERMINAL_*` in the
environment no longer reach the terminal tool, also not after later config reloads. Model
access, memory, skills and the session store stay the profile's, on the host.

With `--sandbox-config` the probe's `config_sha256` is the run file's hash (and
`"mode": "sandbox-config"`); it is `null` when any terminal setting the run reads differs from
the file (`overrides`). `--tui` and `--worktree` are refused with `--sandbox-config`: both
leave the process the policy is pinned in. Without the flag nothing changes.

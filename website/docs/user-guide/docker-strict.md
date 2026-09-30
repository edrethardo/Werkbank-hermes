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

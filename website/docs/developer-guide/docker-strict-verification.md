# WB855 strict Docker implementation verification

Branch: `wb855/docker-strict`; base: `8a71bd301e538c2cb216891cf3f8046063a66be8`.
Implementation and tests are confined to the prepared scratch worktree. No live
profile configuration, gateway restart, merge or push is part of this change.

## Decision

Opt-in, terminal-plus-container-text-reader model-tool policy rather than
attempting to sandbox every host tool individually. The dedicated `read_file`
path avoids host parsers, metadata, extraction and hooks. File mutation/search,
`execute_code` (its ordinary paths include host kernels), browser/web/vision,
process management, delegation, MCP/connectors, and agent-inline tools are
otherwise denied. Terminal commands can edit files and run Python inside Docker.
The Docker argument allowlist is empty: dedicated typed configuration covers
binds, resources, environment and networking.

## Parent's independent runtime verification

After the initial unit shard below, the parent added the real-container
`tests/tools/test_docker_strict_live.py` and reusable
`scripts/verify_docker_strict.py`. The probe checks attempted secret reads and
SSH-directory enumeration without printing any bytes, socket absence, non-root
UID, all three cgroup limits, a real HTTPS Git push refusal with network=none,
and real `read_file` container success plus host-canary failure. Inspect must
show exactly the explicitly approved bind source. Cleanup is awaited.

The parent ran 11 Docker-related files with the canonical runner and
`HERMES_TEST_IMAGE=wb855-strict-test:local`: **184 passed, 0 failed** (including
36 strict-policy cases and the live container case). Observed: UID 1000,
`memory.max=268435456`, `cpu.max=100000 100000`, `pids.max=256`, push exit 128.
The standalone receipt is retained in the Werkbank branch as
`docs/dev/wb855-strict-evidence.json`. A real container probe is NOT a real
model-driven ticket run, nor proof that the whole host controller is confined.

## Initial unit verification receipt

Initial targeted command (canonical runner, not direct pytest):

```sh
HERMES_TEST_SCRATCH_ROOT=/home/aaron/.hermes/profiles/werkbank/cache/scratch/wb855-tests \
 scripts/run_tests.sh \
 tests/tools/test_docker_strict.py \
 tests/tools/test_docker_environment.py \
 tests/tools/test_docker_cgroup_limits.py \
 tests/tools/test_terminal_config_env_sync.py \
 tests/tools/test_docker_network_config.py \
 tests/tools/test_docker_session_isolation.py \
 tests/tools/test_registry.py \
 tests/tools/test_model_tools.py \
 tests/tools/test_code_execution.py \
 tests/tools/test_file_tools.py \
 tests/tools/test_file_tools_container_config.py \
 tests/agent/test_tool_executor_contextvar_propagation.py -j 2
```

Result: **342 passed, 0 failed, 2 skipped**, 12 files. New strict file:
**35 passed**. Skips are Windows-only file tests on Linux. `git diff --check`
is also required before commit. Full-suite comparison and real-container
acceptance belong to the parent task; this result is not a full-suite claim.
No actual Docker containers were created by these mocked-constructor tests.

The new profile test writes disposable YAML and exercises real scope resolution
A→B→A, backend configuration shaping and constructor dispatch. The tool audit
iterates every registered tool plus unknown/bridge names through both common
and registry dispatch; direct file/code and agent-inline paths are also checked.

## Issues found during implementation

- Existing cgroup failure handling is fail-open. Strict refuses workload creation;
  the probe itself receives non-root/no-cap/network-none/entrypoint restrictions.
- Existing default binds include skill credentials/cache; strict never invokes
  those helpers. Image `VOLUME` declarations also create implicit mounts, so
  strict requires a present inspectable image with no such declarations.
- Cached non-strict environments and task backend overrides can otherwise bypass
  a newly enabled setting. Strict refuses the cached object or non-Docker plan.
- Enforcing a non-root user makes the legacy `/root` cwd unusable. Strict maps
  the default to `/tmp` in both configuration resolution and the constructor.
- Initial new tests exposed a `/var/run` lexical-parent check lost by canonicalizing
  everything first, and Unix socket fixture paths exceeding `sun_path`. Keep raw
  and canonical known socket paths; bind test sockets relative to their fixture.
- The canonical runner originally chose `/var/tmp` despite the shell's scratch
  setting. Added opt-in `HERMES_TEST_SCRATCH_ROOT` forwarding; final runs use the
  approved scratch root. Default runner behavior stays unchanged.

## Open security scope

This is **not certification of a complete hostile-user sandbox**. The trusted
host controller still owns prompt/context loading, plugins/middleware, logs,
session state and media/attachment delivery. In particular, arbitrary final-answer
media paths need a separate delivery-path audit or controller confinement.
Network isolation is opt-in (`docker_network: false`); live bind-source mutation
by trusted host processes is outside preflight validation; disk quota support is
still best-effort. See `user-guide/docker-strict.md` for deployment restrictions
and the parent probe API. Do not advertise full hard isolation until those
controller and delivery paths have been independently addressed.

# WB855 strict Docker implementation verification

Branch: `wb855/docker-strict`; base: `8a71bd301e538c2cb216891cf3f8046063a66be8`.
Implementation and tests are confined to the prepared scratch worktree. No live
profile configuration, gateway restart, merge or push is part of this change.

## Decision

Opt-in, terminal-only model-tool policy rather than attempting to sandbox each
host tool individually. This intentionally denies file tools and `execute_code`
(their ordinary paths include host parsers/kernels), browser/web/vision tools,
process management, delegation, MCP/connectors, and agent-inline tools. The
existing terminal can perform file operations and run Python in the container.
The Docker argument allowlist is empty: dedicated typed configuration covers
binds, resources, environment and networking.

## Verification receipt

Final targeted command (canonical runner, not direct pytest):

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

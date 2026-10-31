---
title: Execution Sandbox & Tool Policies
last_updated: 2026-10-30
<<<<<<< HEAD
last_verified_sha: 564a6a1
=======
last_verified_sha: 629fee4
>>>>>>> 9cf66a6d8f2e01379210f7c641a4e445227231f8
sources:
  - src/agentflow/tools/sandbox.py
  - src/agentflow/tools/policy.py
  - src/agentflow/tools/builtin.py
  - src/agentflow/agents/agent.py
  - src/agentflow/orchestrator/engine.py
  - src/agentflow/core/models.py
  - src/agentflow/config.py
status: current
---

# Execution Sandbox & Tool Policies

> **Why it is built this way:** [ADR-009](../../adr/009-sandbox-and-tool-policies.md).

Two layers sit between a model's tool request and its effect:

1. **Policies** ([`tools/policy.py`](../../../src/agentflow/tools/policy.py)) decide
   whether a call may run at all. They are checked in `Agent._checked_call_tool()`
   before dispatch.
2. **The sandbox** ([`tools/sandbox.py`](../../../src/agentflow/tools/sandbox.py))
   decides where agent-issued code runs (Managed Agents' "hands"). Only the execution
   tools use it.

## Sandbox

`Sandbox` protocol: `async run(argv, *, timeout_s) -> ExecResult(exit_code | None,
output, timed_out)` and `async close()`. Callers are `_run_in_sandbox()` in
`tools/builtin.py`, used by `bash_exec` (`["sh", "-c", cmd]`), `bash_exec_readonly`
(after `_check_readonly_command()`) and `python_exec`
(`[python_interpreter(), "-c", code]`).

**Credential-holding tools** (web search with `TAVILY_API_KEY`, MCP servers, `fetch_url`,
`download_document`) are **not** sandboxed. They run in the harness process, so a sandbox
never holds a secret. File tools (`file_read`/`file_write`) stay host-side on the
workspace path, which the Docker backend bind-mounts.

| Backend (`SANDBOX_BACKEND`) | Behaviour |
| --- | --- |
| `local` (default) | `LocalSandbox`: a subprocess in the workspace with `local_env()`: allowlisted vars + `SANDBOX_ENV_PASSTHROUGH`, `HOME` = workspace, no credentials. `start_new_session=True`, and a timeout `killpg`s the whole group. Previously a timed-out command kept running. Not an OS boundary: a command can still read files outside the workspace. |
| `docker` | `DockerSandbox`: one container per run (`agentflow-<run>-<rand>`), started **lazily** on the first `run()` with `docker run --detach --rm … sleep infinity`. Flags: workspace bind-mounted at `/workspace` (also the workdir and `HOME`), `--cap-drop ALL`, `--security-opt no-new-privileges`, `--memory`/`--cpus`/`--pids-limit`, `--user uid:gid`, `--network` (`SANDBOX_DOCKER_NETWORK`, default `bridge`; `none` isolates it). No host env except `SANDBOX_ENV_PASSTHROUGH` names. Each command is `docker exec … timeout -k 5 <s> argv`; GNU timeout exit codes 124/137 → `timed_out`. `close()` = `docker rm --force`. |

- **Fail closed.** If the docker CLI is missing or the container won't start, the call
  raises `SandboxUnavailableError`, and the tool returns `Error: sandbox unavailable — …`
  without running anything on the host. An unknown backend name also raises.
- **Lifetime.** `engine._run()` arms `_current_sandbox` with a `SandboxProvider(ws_dir,
  run_id)` (lazy `get()`) and closes it in `finally`, logging an `OSError`. A run that
  never executes code never creates a container.
- **Outside a run** (campaign feature planning and regression checks, ad-hoc calls),
  `current_sandbox()` returns `_EphemeralSandbox`: a fresh sandbox per command, closed
  right after, so no containers leak.
- Settings: `SANDBOX_DOCKER_IMAGE` (`python:3.13-slim`, which has `sh`, `python3` and
  coreutils `timeout`), `SANDBOX_DOCKER_PYTHON` (`python3`), `_MEMORY` (`2g`), `_CPUS`
  (`2`), `_PIDS_LIMIT` (`512`). Local `python_exec` still uses `SANDBOX_PYTHON`.
- Smoke-tested against real Docker 27: uid not root, `CapEff: 0`, the API key not
  visible, workspace writes seen on the host, timeouts kill, the container is removed.

## Policies

`build_policies(manifest)` builds a fresh chain per `Agent` invocation, so budgets are
per task attempt. `_checked_call_tool(block, tools, emitter, policies, turn)` runs
`policy.check(tool_name, tool_input) -> reason | None` in order. The first reason blocks
the call. The model gets the reason as the `tool_result`. An `agent:tool_result` event
carries `data.blocked_by` (plus `budget_exhausted: true` for limits), and the tool never
runs. `recall_tool_result` is served before the chain ([context-view](context-view.md)).

Chain order:

1. **`CommandAllowlistPolicy`**, one per manifest `tool_policies` entry with
   `allow_commands` (`ToolPolicyConfig`). `allow_commands` is only valid for
   `bash_exec`/`bash_exec_readonly`, checked by a model validator; a bad manifest is
   skipped at load.
   - `shell_commands()` splits on `| || && ; & |&`, skips `VAR=x` prefixes and leading
     redirections, and takes each stage's command name (basename).
   - **It refuses (fail-closed):** newlines, `` ` ``, `$(`, `<(`, `>(`, and subshell
     parentheses outside single quotes, plus unparseable quoting. All of these could run
     a command the allowlist never sees. `find -exec/-execdir/-ok/-okdir` is refused too.
   - Allowlisting a known command runner (`sh`, `bash`, `env`, `xargs`, `python`, `node`,
     `make`, `npx`, …) logs a warning, since it allows anything.
2. **`CallLimitPolicy`**: `tool_limits`, unchanged message ("Tool budget exhausted …").
   It sits **last**, so a call another policy blocked doesn't use up the budget.

To add a policy kind, implement `ToolPolicy` (`name` and `check`), add a field to
`ToolPolicyConfig`, and build it in `build_policies()`.

## Testing

- `tests/test_sandbox.py`: real local subprocesses (credentials scrubbed, the timeout
  actually kills: a delayed `touch` never lands); Docker flags and the lazy start →
  exec → rm sequence against a fake docker CLI; fail-closed; the engine creates one
  sandbox per run, closes it, and creates none without exec; per-command sandboxes
  outside runs.
- `tests/test_policy.py`: parsing (accepted forms, and every refused construct), the
  allowlist, limits, chain order, config validation, and an agent never executing a
  blocked call.

## Related

- [architecture](../architecture.md) — tool layer and lifecycle.
- [verification-and-checkpoints](verification-and-checkpoints.md) — host-side git on the same bind-mounted workspace.

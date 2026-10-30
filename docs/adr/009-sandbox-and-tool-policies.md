# ADR-009: Sandbox interface for exec tools (Docker fails closed); pre-tool-use policy chain

**Status:** Accepted
**Date:** 2026-10-29

## Context

`bash_exec`/`python_exec` ran on the API host with only environment scrubbing. There was
no OS boundary, and no way to limit which commands an agent may run beyond per-tool call
counts. A timed-out command was also never killed.

## Decision

- Exec tools call `current_sandbox().run(argv, timeout_s)`.
  - `local` (default) keeps host execution, with a scrubbed environment and a
    process-group kill on timeout.
  - `docker` runs one container per run, created **lazily** on the first exec. The
    workspace is bind-mounted, all capabilities are dropped, privilege escalation is
    disabled, memory/CPU/PID limits apply, it runs as the host uid, and it gets no host
    environment.
  - With `docker`, **an unavailable Docker fails the tool** rather than falling back to
    the host.
- **Credential-holding tools (web search, MCP, fetch) stay in the harness process**, so a
  sandbox never sees a secret. File tools stay host-side on the shared mount, which keeps
  checkpoints and the verifier unchanged.
- A per-invocation **policy chain** runs before every tool call: manifest
  `tool_policies` command allowlists, with fail-closed shell parsing (no substitution,
  subshells or newlines), then `tool_limits`.

## Consequences

- An operator can isolate code execution with one setting, and constrain an agent's shell
  through its manifest.
- The bind mount means files move between host and container without copying.
- The container's network defaults to `bridge`, so package installs keep working; `none`
  is available.
- Allowlisting a command runner (`sh`, `env`, `xargs`, `npm run`, …) defeats the
  allowlist. This is warned about but cannot be prevented.

## Alternatives considered

- **Copy files into an unmounted sandbox (pure Managed Agents model):** stronger
  isolation, but file tools, checkpoints and the verifier would all need a sync protocol.
  Deferred.
- **Fall back to local when Docker is down:** silently loses the isolation the operator
  asked for. Rejected.
- **Regex-based allowlist (as in the reference demo):** easy to bypass with newlines or
  substitution. Rejected for a tokenising, fail-closed parser.

KB: [subsystems/sandbox-and-policies](../kb/subsystems/sandbox-and-policies.md)

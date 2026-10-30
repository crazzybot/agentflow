# ADR-002: Per-run and named workspaces

**Status:** Accepted
**Date:** 2026-10-29

## Context

Every run's tools wrote into one global `workspace/` directory, so two concurrent runs
could overwrite each other's files. That directory also held long-lived user projects
(`app-gateway/`, `semantic-kb/`, …) that successive runs keep working on. Isolating
every run strictly would have broken that workflow.

## Decision

- A run gets a **fresh** `workspace/runs/<run_id>/` unless the request names a
  workspace.
- `workspace: "<name>"` runs in `workspace/<name>/`. Names are a single safe path
  segment, and `runs` is reserved.
- Followups inherit the prior run's workspace.
- Runs that share a workspace are serialised by a per-workspace lock.
- The resolved path is recorded in `meta.json`, and tools read it from a `ContextVar`.

## Consequences

- Concurrent unrelated runs can no longer collide. Existing projects are reached by name.
- A run without `workspace` no longer sees the root's projects. This was a behaviour
  change.
- `CodeAgent` was told to treat the workspace as the project root, so a named workspace
  doesn't become `app-gateway/app-gateway/`.
- The lock is process-local. Two replicas can still enter one named workspace at once.

## Alternatives considered

- **Opt-in `WORKSPACE_MODE=shared|per_run`:** zero behaviour change, but there's no way to
  pick a project per request. Rejected.
- **Strict per-run always:** simplest isolation, but existing projects become
  unreachable except through followups. Rejected.
- Chosen by the project owner from these options.

KB: [architecture § Workspaces](../kb/architecture.md#workspaces)

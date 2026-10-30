# ADR-007: Workspace checkpoints in a shadow git repository

**Status:** Accepted
**Date:** 2026-10-29

## Context

A failed attempt's partial edits stayed in the workspace for the retry to stumble over,
and there was no record of what each subtask changed. Named workspaces are often the
user's own git repositories ([ADR-002](002-per-run-and-named-workspaces.md)).

## Decision

- Snapshot each run's workspace into a **separate** repository at
  `<runs_dir>/<run_id>/checkpoints.git`, with the workspace as its work tree. There is a
  baseline commit at run start or resume, then one commit per finished subtask.
- **Never touch the workspace's own `.git`.** Honour its `.gitignore`, add default
  excludes, and run git isolated from user and system config, hooks and signing.
- Before a retry or fallback, restore **only the files the failed attempt wrote**
  (`files_written`) to the subtask's starting snapshot.
- If git is missing or fails, checkpoints are disabled for that run instead of the run
  failing.

## Consequences

- There's a per-subtask audit trail (`GET /runs/{id}/checkpoints`), and failed attempts
  are cleaned up.
- **Rollback is narrow by design:** changes made through `bash_exec`/`python_exec`
  aren't rolled back.
- A checkpoint can include a parallel sibling's half-written files, so its changed-file
  list is attribution by time, not by agent.
- Every run initialises a repository, and the first snapshot of a large named workspace
  takes noticeable time.

## Alternatives considered

- **Commit into the project's own repo/branch:** intrusive to user projects. Rejected.
- **Reset the whole workspace on failure:** parallel subtasks share it, so this would
  wipe their in-progress work. Rejected.

KB: [subsystems/verification-and-checkpoints](../kb/subsystems/verification-and-checkpoints.md)

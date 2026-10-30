---
name: session-wrap
description: End-of-session knowledge base maintenance. Review this session's git
             changes, propose CLAUDE.md and docs/kb/ updates, identify skill extraction
             candidates, and write a dated entry to docs/session-log.md. Invoke when
             the user says "wrap up", "end session", "update the KB", or similar.
invocation: manual
---

# Session Wrap

End-of-session review. Runs in six steps; confirms before writing anything.

## Git Context

!`git log --oneline -15`

!`git diff HEAD~1 --stat 2>/dev/null || git diff --cached --stat 2>/dev/null || echo "(no committed changes this session)"`

!`git status --short`

## Step 1 — Discover this session's changes

Review the git log, diff stat, and status above. Identify:
- Which modules/packages were touched
- What new files were created, deleted, or renamed
- Which KB docs (`docs/kb/`) are in scope of `sources` fields that changed

## Step 2 — Analyse for KB-worthy facts

For each changed area, ask:
- Is there a new build/run/test command?
- Was a convention established or changed?
- Was an architectural decision made?
- Did a "Do not" rule emerge from something that went wrong?
- Was a new dependency, tool, or framework added?
- Did a new module or subsystem appear that needs a `docs/kb/subsystems/` doc?

## Step 3 — Propose CLAUDE.md edits

Present specific, minimal additions as a numbered list:

> "Proposed addition to **[Section Name]**: `<exact text>`"
> "Reason: <one sentence>"

Do NOT propose changes that:
- Are already in CLAUDE.md
- Are temporary / session-specific
- Are better expressed as a skill file
- Duplicate what is already in `docs/kb/`

## Step 4 — Identify skill extraction candidates

If any multi-step procedure appeared more than twice in this session, or if a
CLAUDE.md section has grown into a procedure, propose it as a new skill file at
`.claude/skills/<name>/SKILL.md`.

## Step 5 — Check KB freshness

For each `docs/kb/` doc whose `sources` overlap with changed files, run the drift
check and flag any doc that needs reconciliation:

```bash
git diff --stat <doc-last_verified_sha> HEAD -- <doc-sources>
```

(A tree diff, not `git log sha..HEAD`: PRs are squash-merged, so a recorded SHA is
often not an ancestor of `main`.)

If this session changed a concept, component, flow, storage location or deployment
option, check that `docs/guide/whats-new.md` has a dated plain-language entry for it,
and that the affected guide page was updated. Propose the entry if not.

Also list any architectural decision made this session that has no ADR in
`docs/adr/` (see its README § "When to write one"), and propose one for each.

If any doc is stale and the `update-kb` skill has not been run yet, recommend
running it before closing the session.

## Step 6 — Write session log entry

Append to `docs/session-log.md`:

```
## Session: <YYYY-MM-DD>
**Files changed:** <comma-separated list of key files>
**Decisions:** <bullet list of choices made>
**Open questions:** <bullet list, or "none">
**KB updates applied:** <list of changes made, or "none">
```

## Confirm before writing

Show all proposed changes (CLAUDE.md edits, new skill files, KB updates, session log
entry) and ask: **"Apply these updates? (yes / edit / skip)"**

Only write files after the user confirms.

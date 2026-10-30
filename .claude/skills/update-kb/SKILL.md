---
name: update-kb
description: Use at the end of any implementation or improvement task to reconcile the docs/kb knowledge base with code changes and refresh freshness metadata before declaring the task complete.
---

# Update Knowledge Base

Run this after completing an implementation/improvement task, before declaring it
done. It keeps `docs/kb/` current with the code.

## Checklist (create one todo per item)

1. **List what changed.** Get the paths your task touched:
   `git diff --name-only <task-start-sha>..HEAD` (or the working tree if uncommitted).

2. **Find affected docs.** For each KB doc, read its frontmatter `sources`. A doc
   is affected if any changed path is at or under one of its `sources` entries.

3. **Reconcile prose.** For each affected doc, update the body so it matches the
   new code reality — names, flow, recipes. Do not copy ADR rationale into the KB;
   link the ADR instead. Never update anything under `docs/archive/` — it is frozen.

4. **Refresh metadata.** On every doc you reconciled, set `last_updated` to today
   and set `last_verified_sha` to current HEAD (`git rev-parse --short HEAD`).

5. **New subsystem?** If the change introduced a whole subsystem worth its own
   doc, create `docs/kb/subsystems/<name>.md` (same frontmatter contract) and link
   it from `docs/kb/index.md`.

5a. **ADR needed?** Check the task against `docs/adr/README.md` § "When to write one".
   Did it choose between real alternatives, add a hard-to-undo constraint (format,
   protocol, security boundary, behaviour-changing default), or deliberately skip
   something obvious? If so, write `docs/adr/NNN-slug.md` with **what was weighed**, and
   add it to the ADR index. Link it from the KB doc(s) the decision shows up in. If the
   task changed or reversed an earlier ADR's decision, amend or supersede that ADR as
   the README describes. Never rewrite an accepted ADR's body.

5b. **Human guide.** `docs/guide/` is the human-readable architecture guide. It
   changes only when a **concept, component, flow, storage location or deployment
   option** changes. Bug fixes and refactors don't touch it. If this task did one of
   those things:
   - Update the affected page(s) in plain language: no function-level detail (that
     belongs in the KB, so link to the KB instead). Keep Mermaid diagrams in sync,
     since they describe behaviour.
   - Add a dated entry at the top of `docs/guide/whats-new.md` saying what changed for
     someone using or extending AgentFlow, linking the ADR (if any) and the KB doc.
   - Refresh that page's `last_updated`/`last_verified_sha`.
   Guide pages carry the same frontmatter as KB docs and are covered by the drift sweep
   (step 6). When a guide page drifts, its `sources` are broad on purpose: check
   whether the page is still **true at its level of abstraction**. Usually it is, and
   only the metadata needs refreshing.

6. **Drift sweep.** For EVERY doc in `docs/kb/` and `docs/guide/`, run the drift query:
   `git diff --stat <last_verified_sha> HEAD -- <that doc's sources>`
   Non-empty output = the doc is behind. (Use a tree diff, not `git log sha..HEAD`:
   PRs are squash-merged, so a SHA recorded on a PR branch is never an ancestor of
   `main` and `git log` would list every later commit as unseen.) Fix it now, or if it genuinely cannot be
   fixed in this task, set its frontmatter `status: stale` so the next run catches it.

7. **Validate.** For each doc you changed, confirm frontmatter still has all five
   keys in order (`title`, `last_updated`, `last_verified_sha`, `sources`,
   `status`), every `sources` path exists, and internal links resolve.

8. **Commit** the KB updates alongside (or right after) your task's changes.

## Drift query reference

```bash
SHA=<doc's last_verified_sha>
git diff --stat ${SHA} HEAD -- <space-separated sources from that doc>
```
Empty output means the doc is current for its declared sources.
```

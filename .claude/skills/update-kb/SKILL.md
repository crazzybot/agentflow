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
   new code reality — names, flow, recipes. Do not duplicate the human docs in
   `docs/` (design.md, gap analysis, presentation); link instead.

4. **Refresh metadata.** On every doc you reconciled, set `last_updated` to today
   and set `last_verified_sha` to current HEAD (`git rev-parse --short HEAD`).

5. **New subsystem?** If the change introduced a whole subsystem worth its own
   doc, create `docs/kb/subsystems/<name>.md` (same frontmatter contract) and link
   it from `docs/kb/index.md`.

6. **Drift sweep.** For EVERY doc in `docs/kb/`, run the drift query:
   `git log --oneline <last_verified_sha>..HEAD -- <that doc's sources>`
   Non-empty output = the doc is behind. Fix it now, or if it genuinely cannot be
   fixed in this task, set its frontmatter `status: stale` so the next run catches it.

7. **Validate.** For each doc you changed, confirm frontmatter still has all five
   keys in order (`title`, `last_updated`, `last_verified_sha`, `sources`,
   `status`), every `sources` path exists, and internal links resolve.

8. **Commit** the KB updates alongside (or right after) your task's changes.

## Drift query reference

```bash
SHA=<doc's last_verified_sha>
git log --oneline ${SHA}..HEAD -- <space-separated sources from that doc>
```
Empty output means the doc is current for its declared sources.
```

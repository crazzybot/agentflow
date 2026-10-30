# Archive

These are historical documents, **not maintained**. They describe AgentFlow as it was when
each one was written. For the current system, read [`docs/kb/`](../kb/index.md). For the
reasons behind architectural choices, read [`docs/adr/`](../adr/README.md).

Files keep their original content, so some paths and claims in them are out of date.

| Document | Written | What it was | Why archived |
| --- | --- | --- | --- |
| [`fix-plan.md`](fix-plan.md) | 2026-07-10 | Fixes drawn from two research-run analyses: hard tool-call budgets, `file_write` line counts, and others | Every fix is implemented (e.g. `tool_limits`, now part of the policy chain) |
| [`presentations/design.md`](presentations/design.md) | 2026-07 (last edited 2026-09-30) | Original system design document | Predates per-run workspaces, the session log, resume, verification, campaigns, sandboxes and workers. The KB is the current description |
| [`presentations/agentflow_gap_analysis.md`](presentations/agentflow_gap_analysis.md) | 2026-07-09 | Gap analysis and roadmap (Gaps 1–9) | Most gaps are addressed, wholly or partly: state (Redis, leases, workers), context growth (result caps, snipping), model routing (cost tiers), and HITL as a safety control (tool policies). The rest are out of date as written |
| [`presentations/agentflow_presentation.html`](presentations/agentflow_presentation.html), [`…_notes.md`](presentations/agentflow_presentation_notes.md), [`…_notes_concise.md`](presentations/agentflow_presentation_notes_concise.md) | 2026-07-09 | Slide deck and speaker notes based on the design and gap analysis | They present the July system |
| [`superpowers/specs/…-agent-knowledge-base-design.md`](superpowers/specs/2026-07-09-agent-knowledge-base-design.md), [`superpowers/plans/…-agent-knowledge-base.md`](superpowers/plans/2026-07-09-agent-knowledge-base.md) | 2026-07-09 | Design and implementation plan for `docs/kb/` | Done: the KB exists and is maintained by the `update-kb` skill |

Archive a document when the work it plans is done or it no longer describes the
system. Move it with `git mv` so its history follows, add a row here, and repoint any
live links to it.

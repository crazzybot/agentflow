---
title: AgentFlow Architecture Guide
last_updated: 2026-10-30
last_verified_sha: 629fee4
sources:
  - docs/guide/overview.md
  - docs/guide/concepts.md
  - docs/guide/architecture.md
  - docs/guide/flows.md
  - docs/guide/data-and-storage.md
  - docs/guide/deployment-modes.md
status: current
---

# AgentFlow Architecture Guide

This guide is for people who want to understand how AgentFlow fits together. It covers
the concepts, the main components, what happens during a run, and how the system can be
deployed.

It sits alongside two other sets of docs:

- **This guide**: the big picture, in plain language with diagrams.
- **[The knowledge base](../kb/index.md)**: precise, current implementation detail
  (function names, edge cases, invariants). It's written for coding agents, but it's the
  place to go once you need exact behaviour.
- **[Architecture Decision Records](../adr/README.md)**: *why* each major design is the
  way it is, and which alternatives were rejected.

Where this guide simplifies, it links to the KB for detail and to an ADR for the reasoning.

## Reading order

| Page | Read it to learn… |
| --- | --- |
| [Overview](overview.md) | What AgentFlow is and what it can do, on one page |
| [Concepts](concepts.md) | The vocabulary: run, subtask, workspace, session log, campaign, worker… |
| [Architecture](architecture.md) | The components and how they connect |
| [Flows](flows.md) | Step by step, what happens in a run, a retry, a crash, a campaign, a remote call |
| [Data & storage](data-and-storage.md) | What is stored where (files, Redis), and what is durable |
| [Deployment modes](deployment-modes.md) | Single process, replicas, workers, Docker sandbox, and the settings that matter |
| [What's new](whats-new.md) | Dated changes to the architecture, in plain words |

If you have been away for a while, start with **[What's new](whats-new.md)**.

## Keeping this guide current

Each page lists the code it describes in its frontmatter (`sources`), with the commit it
was last checked against (`last_verified_sha`). The `update-kb` skill runs the same
drift check here as for the KB. The guide changes only when a **concept, component,
flow or deployment option** changes. A bug fix doesn't touch it; a new subsystem does,
and every such change also gets a line in [What's new](whats-new.md).

Diagrams are [Mermaid](https://mermaid.js.org/), so they're text that can be reviewed in
diffs. They render on GitHub and in VS Code's Markdown preview.

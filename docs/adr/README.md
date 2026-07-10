# Architecture Decision Records

Lightweight records of significant architectural choices: what was decided, why,
and what alternatives were considered.

## Format

Each ADR is a single Markdown file named `NNN-slug.md` (zero-padded, hyphen-separated).

```markdown
# ADR-NNN: Title

**Status:** Accepted | Deprecated | Superseded by ADR-XXX
**Date:** YYYY-MM-DD

## Context
Why did this decision need to be made?

## Decision
What was decided?

## Consequences
What changes as a result? What becomes easier or harder?

## Alternatives considered
What else was on the table and why was it rejected?
```

## Index

| ADR | Title | Status |
|-----|-------|--------|
| [001](001-redis-state-backend.md) | Redis state backend for multi-replica runs | Accepted |

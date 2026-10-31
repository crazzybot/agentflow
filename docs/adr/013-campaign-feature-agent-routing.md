# ADR-013: Route each campaign feature to an agent

**Status:** Accepted
**Date:** 2026-10-30

## Context

A campaign named one `agent_id` (default `CodeAgent`) and ran every feature on it. A
project whose spec mixes kinds of work, such as code plus user documentation, had its
prose features written by the coding agent. The run planner already solves this for
ordinary runs: it sees the agent roster (`AgentRegistry.summary()`) and routes each
subtask to a specialist.

## Decision

- **The feature planner routes.** Unless the campaign pins an agent, it receives the
  agent roster and outputs an `agentId` for each feature. The code checks the choice
  against the registry. A missing or unknown choice falls back to
  `CAMPAIGN_DEFAULT_AGENT` (`CodeAgent`).
- **One agent per feature.** A feature that needs two kinds of work is split into two
  features in dependency order. A session is still a one-subtask run.
- **`CampaignRequest.agent_id` becomes optional.** When it is omitted (the new default),
  features are routed. When it is set, every feature is pinned to that agent, which is
  the old behaviour. Stored campaigns that have `agent_id` set keep running as pinned.
- **Routing is not part of a feature's immutable definition.** `Feature.agent_id` is
  stored in the ledger, but `save_features()` guards only title, description and checks.
  An operator retry may reassign the agent. Agents still can't touch the ledger.
- Sessions and regression checks use the feature's agent. If that agent is no longer
  registered, they fall back to the pinned or default agent.

## Consequences

- Specialists build what they are suited for, and the progress list in each session's
  instruction shows which agent owns each feature.
- A ledger is now tied to agent ids. If a manifest is removed, its features silently
  fall back, with a warning in the log.
- Session cost controls ([ADR-011](011-campaign-session-cost-controls.md)) apply to
  every agent: the first attempt runs on `CAMPAIGN_SESSION_MODEL`, whatever the agent's
  manifest says.
- Clients that relied on the implicit `CodeAgent` default now get routed campaigns.
  To keep the old behaviour, pass `"agent_id": "CodeAgent"`.

## Alternatives considered

- **Let each session run the full planner**, so one feature becomes a multi-agent DAG.
  This is more flexible, but it adds a planning call to every session and makes it harder
  to say which agent the verifier and the escalation policy judge. Splitting the
  feature gets the same result within the ledger model.
- **Treat the agent as part of the immutable definition.** Rejected: a feature stuck on
  the wrong specialist could only be retried on that same specialist. Who does the work
  doesn't change what passing means, and the checks do stay fixed.
- **Keep `CodeAgent` as the default and make routing opt-in.** Rejected: routing is the
  better behaviour for mixed specs, and a pure-code spec routes to `CodeAgent` anyway.

## Related

- [ADR-008](008-campaign-ledger-in-code.md): campaigns and the immutable ledger.
- KB: [campaigns](../kb/subsystems/campaigns.md).

# Multi-Agent Orchestration System — Implementation Design

> **Version:** 1.0  
> **Purpose:** Implementation reference for Claude Code  
> **Stack:** Node.js / Python FastAPI · Claude claude-sonnet-4-20250514 · Redis Streams · SSE

---

## Table of Contents

1. [Design Goals](#1-design-goals)
2. [High-Level Architecture](#2-high-level-architecture)
3. [Orchestrator Engine](#3-orchestrator-engine)
4. [Agent Design](#4-agent-design)
5. [Message Protocols](#5-message-protocols)
6. [Streaming](#6-streaming)
7. [Implementation Plan](#7-implementation-plan)
8. [Definition of Done](#8-definition-of-done)

---

## 1. Design Goals

| Goal | Description |
|---|---|
| **Specialization** | Each agent has a scoped system prompt, toolset, and skill set — no generalist ambiguity |
| **Isolation** | Agents receive only the context they need; they cannot directly communicate with other agents |
| **Composability** | Orchestrator dynamically routes subtasks to the best-fit agent at runtime |
| **Transparency** | Every planning step, dispatch, and agent action streams to the client in real time |
| **Extensibility** | New agents register via manifest — no orchestrator code changes required |
| **Resilience** | Retry logic, fallback agents, and timeout handling are orchestrator-level concerns |

### Technology Stack

| Component | Choice |
|---|---|
| Orchestrator Runtime | Node.js or Python FastAPI |
| Agent LLM | `claude-sonnet-4-20250514` (per agent invocation) |
| Task Bus | Redis Streams or in-process queue |
| Streaming Transport | Server-Sent Events (SSE) |
| Agent Registry | JSON manifests + runtime registry |
| Observability | OpenTelemetry + structured logs |

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                          CLIENT                                  │
│   POST /run  { task, context }        GET /run/:id/stream (SSE) │
└──────────┬──────────────────────────────────────────────────────┘
           │ HTTP                            ▲ SSE stream events
           ▼                                │
┌──────────────────────────────────────────┴───────────────────────┐
│                       ORCHESTRATOR SERVICE                        │
│                                                                   │
│  ┌─────────────┐    ┌──────────────────┐    ┌─────────────────┐  │
│  │  Task       │    │  LLM Planner     │    │  Stream         │  │
│  │  Receiver   │───▶│  (decomposition) │───▶│  Emitter        │  │
│  └─────────────┘    └────────┬─────────┘    └─────────────────┘  │
│                              │ Plan JSON                          │
│                   ┌──────────▼──────────┐                        │
│                   │  Execution Engine   │                        │
│                   │  (dep. graph +      │                        │
│                   │   scheduler)        │                        │
│                   └──────┬──────┬───────┘                        │
│                          │      │  Back-channel requests         │
│                 dispatch │      │                                │
│                          ▼      ▼                                │
│              ┌───────────────────────────────┐                   │
│              │       Agent Registry          │                   │
│              │  ResearchAgent  CodeAgent     │                   │
│              │  DataAgent      WriterAgent   │                   │
│              │  PlannerAgent   [custom...]   │                   │
│              └───────────────────────────────┘                   │
└──────────────────────────────────────────────────────────────────┘
           │ Task Envelope (per agent, isolated)
           ▼
┌──────────────────────┐   ┌──────────────────────┐
│  AGENT PROCESS       │   │  AGENT PROCESS        │
│  ┌────────────────┐  │   │  ┌────────────────┐   │
│  │ System Prompt  │  │   │  │ System Prompt  │   │
│  │ (specialized)  │  │   │  │ (specialized)  │   │
│  ├────────────────┤  │   │  ├────────────────┤   │
│  │ Tool Registry  │  │   │  │ Tool Registry  │   │
│  │ (scoped)       │  │   │  │ (scoped)       │   │
│  ├────────────────┤  │   │  ├────────────────┤   │
│  │ Skill Set      │  │   │  │ Skill Set      │   │
│  └────────────────┘  │   │  └────────────────┘   │
│     Claude API call  │   │     Claude API call    │
└──────────────────────┘   └──────────────────────┘
```

### Key Architectural Decisions

**Agents are stateless — context is in the envelope.**  
Agents receive all context via Task Envelope. No agent holds state between invocations. This enables horizontal scaling and simplifies retry logic.

**Orchestrator holds the shared execution context.**  
The orchestrator maintains a per-run context store. When agents need results from other agents, they request it through the orchestrator — never directly.

**Planning is a separate LLM call, not hardcoded routing.**  
The orchestrator uses a planning LLM call with the agent registry as context to produce a structured execution plan (JSON). Routing is adaptive without code changes.

**Streaming is first-class, not bolted on.**  
Every internal state transition emits a typed SSE event. The client receives a live trace of execution.

---

## 3. Orchestrator Engine

### Pipeline Steps

**Step 01 — Task Intake**  
Receive top-level task + user context. Create a run ID. Open SSE channel. Emit `run:started` event.

**Step 02 — LLM Planning Pass**  
Call Claude with the task + agent registry manifest. Request a structured JSON plan: list of subtasks, assigned agent, dependencies, and expected output shape.

**Step 03 — Dependency Graph**  
Parse the plan into a DAG. Identify which subtasks can run in parallel vs. which must wait for upstream results.

**Step 04 — Scheduling Loop**  
Continuously check for ready subtasks (all dependencies met). Dispatch each ready subtask to the assigned agent via Task Envelope.

**Step 05 — Context Propagation**  
When a subtask completes, store its structured output in the run context store. Make it available for downstream subtasks.

**Step 06 — Back-Channel Handling**  
If an agent emits an `info_request`, the orchestrator resolves it — either from the context store or by invoking another agent — then resumes the requesting agent with the result.

**Step 07 — Assembly & Completion**  
When all subtasks complete, run a final assembly step (optional LLM pass to merge outputs). Emit `run:complete`. Close the SSE stream.

### LLM Planning Prompt Template

```
SYSTEM:
You are an orchestration planner. Given a task and a list of available agents,
produce a JSON execution plan. Never invent agents not in the registry.

USER:
Task: "{{ user_task }}"

Available Agents:
{{ agent_registry_summary }}

Produce a JSON plan with this schema:
{
  "subtasks": [
    {
      "id": "st_1",
      "agentId": "ResearchAgent",
      "instruction": "...",
      "dependsOn": [],
      "expectedOutput": "structured market data"
    },
    ...
  ]
}
```

### Resilience Policies

| Policy | Behaviour |
|---|---|
| **Retry** | Up to 3 retries per subtask with exponential backoff. Retry only on transient errors (timeout, rate limit). |
| **Fallback** | If primary agent fails after retries, orchestrator checks registry for a fallback agent with overlapping capabilities. |
| **Timeout** | Each subtask has a configurable timeout. On timeout, partial results are saved and the run continues if dependencies allow. |

---

## 4. Agent Design

### Agent Manifest Schema

```json
{
  "agentId": "ResearchAgent",
  "version": "1.2.0",
  "domain": "Information Retrieval",
  "capabilities": ["web_research", "document_synthesis", "citation"],
  "tools": ["web_search", "fetch_url", "arxiv_search"],
  "inputSchema": {
    "instruction": "string",
    "context": "object",
    "constraints": {
      "maxTokens": "number",
      "timeoutMs": "number"
    }
  },
  "outputSchema": {
    "structured": "object",
    "text": "string",
    "citations": "string[]"
  },
  "fallbackFor": [],
  "maxConcurrency": 3
}
```

### Built-in Agents

#### ResearchAgent
- **Domain:** Information Retrieval  
- **System Prompt:** You are an expert research analyst. Your sole purpose is to gather, synthesize, and return structured information on a given topic. Always cite sources. Return structured JSON matching the requested output schema.  
- **Tools:** `web_search`, `fetch_url`, `arxiv_search`, `wikipedia`  
- **Skills:** source evaluation, summarization, citation extraction

#### CodeAgent
- **Domain:** Software Engineering  
- **System Prompt:** You are a senior software engineer. You write, review, debug, and explain code across multiple languages and paradigms. Always prefer working, tested code over explanation.  
- **Tools:** `bash_exec`, `file_read`, `file_write`, `lint`, `test_runner`  
- **Skills:** code generation, debugging, architecture review

#### DataAgent
- **Domain:** Data Analysis  
- **System Prompt:** You are a data scientist specializing in analysis, visualization, and statistical reasoning. Return findings as structured JSON with supporting charts or tables where relevant.  
- **Tools:** `python_exec`, `sql_query`, `chart_gen`, `csv_parse`  
- **Skills:** statistical analysis, visualization, anomaly detection

#### WriterAgent
- **Domain:** Content & Communication  
- **System Prompt:** You are a professional writer and editor. You craft clear, compelling prose tailored to the requested audience and format. Match tone and register precisely to the brief.  
- **Tools:** `spell_check`, `readability_score`, `tone_analyzer`  
- **Skills:** copywriting, editing, tone adaptation, SEO

#### PlannerAgent
- **Domain:** Strategy & Decomposition  
- **System Prompt:** You are a strategic project planner. You break complex goals into actionable steps, estimate effort, and identify dependencies and risks.  
- **Tools:** `dependency_graph`, `timeline_gen`, `risk_model`  
- **Skills:** task decomposition, dependency mapping, risk analysis

### Isolation Contract

- Agents receive context **only** via Task Envelope — never via shared memory or environment variables.
- Agents **cannot** initiate calls to other agents. They can only emit `info_request` back to the orchestrator.
- Each agent invocation is a **fresh LLM call** — no cross-task memory leakage.
- Tool access is **scoped**: the agent manifest declares exactly which tools are available. No others can be invoked.
- Agents return structured output matching their declared `outputSchema` — the orchestrator validates before storing.

---

## 5. Message Protocols

### Task Envelope — Orchestrator → Agent

```json
{
  "taskId": "uuid-v4",
  "parentRunId": "uuid-v4",
  "agentId": "ResearchAgent",
  "instruction": "Gather EV market data for 2020–2025",
  "context": {
    "priorResults": {},
    "sharedMemory": {}
  },
  "constraints": {
    "maxTokens": 4096,
    "timeoutMs": 30000
  }
}
```

### Agent Result — Agent → Orchestrator

```json
{
  "taskId": "uuid-v4",
  "agentId": "ResearchAgent",
  "status": "success | partial | failed",
  "output": {
    "structured": {},
    "text": "Summary of findings..."
  },
  "tokensUsed": 1842,
  "durationMs": 4200
}
```

### Back-Channel Request — Agent → Orchestrator

```json
{
  "type": "info_request",
  "fromAgent": "DataAgent",
  "taskId": "uuid-v4",
  "query": "Need raw CSV from ResearchAgent",
  "requiredFields": ["salesData"],
  "blocking": true
}
```

### SSE Stream Event — Server → Client

```json
{
  "runId": "uuid-v4",
  "seq": 14,
  "ts": 1715123456789,
  "type": "agent_progress | plan | dispatch | agent_query | complete | error",
  "agentId": "DataAgent",
  "payload": {
    "message": "Computing CAGR...",
    "partial": null
  }
}
```

### Full Communication Sequence

```
Client          Orchestrator         Agent A           Agent B
  │                  │                  │                  │
  │─ POST /run ─────▶│                  │                  │
  │                  │── plan() ────────│                  │
  │◀─ SSE open ──────│                  │                  │
  │                  │                  │                  │
  │◀ plan:created ───│                  │                  │
  │                  │── TaskEnvelope ─▶│                  │
  │◀ task:dispatched ─│                  │                  │
  │                  │                  │── (tool calls)   │
  │◀ agent:progress ──│◀─ progress ──────│                  │
  │                  │                  │                  │
  │                  │◀─ info_request ──│                  │
  │                  │── TaskEnvelope ─────────────────────▶│
  │                  │◀─ result ─────────────────────────── │
  │                  │── context ──────▶│                  │
  │                  │◀─ result ────────│                  │
  │◀ task:complete ───│                  │                  │
  │◀ run:complete ────│                  │                  │
  │                  │                  │                  │
```

---

## 6. Streaming

### SSE Event Types

| Event | Description |
|---|---|
| `run:started` | Run created, SSE channel open, metadata emitted |
| `plan:created` | Orchestrator emits the full subtask plan JSON |
| `task:dispatched` | A subtask has been sent to an agent |
| `agent:progress` | Agent emits a mid-task status update or partial output |
| `agent:query` | Agent is requesting additional context from orchestrator |
| `task:complete` | A subtask finished successfully with structured output |
| `task:failed` | A subtask failed; includes error and retry info |
| `run:complete` | All subtasks done; final assembled result attached |
| `run:error` | Unrecoverable orchestrator-level error |

### Client Integration (JavaScript)

```javascript
const source = new EventSource(`/api/run/${runId}/stream`);

source.addEventListener("message", (e) => {
  const event = JSON.parse(e.data);

  switch (event.type) {
    case "plan:created":
      renderPlan(event.payload.subtasks);
      break;
    case "task:dispatched":
      markDispatched(event.payload.taskId, event.payload.agentId);
      break;
    case "agent:progress":
      appendProgress(event.payload.agentId, event.payload.message);
      break;
    case "run:complete":
      renderFinalResult(event.payload.output);
      source.close();
      break;
    case "run:error":
      showError(event.payload.error);
      source.close();
      break;
  }
});
```

### Example Execution Stream

```
00:01  [ORCH]     Task received: 'Produce a market analysis report on EVs'
00:02  [PLAN]     LLM planning pass → decomposing into 4 subtasks
00:03  [DISPATCH] → ResearchAgent: gather EV market data (2020–2025)
00:04  [DISPATCH] → DataAgent: analyze sales trends, compute CAGR
00:05  [AGENT]    [ResearchAgent] Fetching sources… (12 docs found)
00:06  [AGENT]    [ResearchAgent] Synthesizing findings…
00:07  [QUERY]    [DataAgent] → Orchestrator: need raw sales CSV from ResearchAgent
00:08  [ORCH]     Routing data from ResearchAgent → DataAgent
00:09  [AGENT]    [DataAgent] Computing CAGR… result = 28.4%
00:10  [DISPATCH] → WriterAgent: draft executive summary + full report
00:11  [AGENT]    [WriterAgent] Drafting 1,800-word report…
00:12  [DONE]     All subtasks complete — assembling final artifact
```

---

## 7. Implementation Plan

### Phase 1 — Core Infrastructure (Week 1–2)

- [ ] Define `Agent` interface, `AgentManifest` schema, and tool registry
- [ ] Build `AgentRegistry` with capability indexing
- [ ] Implement base message bus (task envelope format)
- [ ] Set up SSE / WebSocket streaming transport layer
- [ ] Create isolated execution context per agent invocation

### Phase 2 — Orchestrator Engine (Week 3–4)

- [ ] LLM-based task decomposition with structured output (JSON plan)
- [ ] Dependency graph builder and topological sort scheduler
- [ ] Context propagation: passing subtask results downstream
- [ ] Back-channel request handling (agent → orchestrator → agent)
- [ ] Retry, fallback, and timeout policies

### Phase 3 — Agent Implementations (Week 5–6)

- [ ] `ResearchAgent` with `web_search` + `fetch_url` + synthesis tools
- [ ] `CodeAgent` with sandboxed bash execution
- [ ] `DataAgent` with Python/SQL runtime
- [ ] `WriterAgent` with grammar + tone tools
- [ ] `PlannerAgent` for meta-decomposition tasks

### Phase 4 — Streaming & Client UX (Week 7)

- [ ] Structured SSE event schema (`type`, `agentId`, `payload`, `timestamp`)
- [ ] Client-side event parser and real-time progress UI
- [ ] Partial result streaming for long-running agents
- [ ] Error event propagation and graceful degradation display

### Phase 5 — Observability & Hardening (Week 8)

- [ ] Distributed trace per orchestration run (OpenTelemetry)
- [ ] Agent cost + token tracking per subtask
- [ ] Prompt injection safeguards on task envelope parsing
- [ ] Load testing: concurrent orchestration runs
- [ ] Deployment: containerized agents, orchestrator service

---

## 8. Definition of Done

| Guarantee | Acceptance Criterion |
|---|---|
| **Correctness** | All subtasks complete with valid structured output matching agent `outputSchema` |
| **Streaming** | Client receives real-time events for every state transition within 200ms |
| **Isolation** | No cross-task context leakage verified via unit tests |
| **Back-channel** | Agent `info_request` resolved and agent resumed correctly |
| **Resilience** | 3 consecutive failed agent invocations trigger fallback without run failure |
| **Observability** | Full trace exportable per run; cost and tokens tracked per subtask |

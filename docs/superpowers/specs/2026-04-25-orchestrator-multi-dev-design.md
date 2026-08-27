# Orchestrator + Multi-Dev Architecture Design

**Date:** 2026-04-25
**Status:** Approved

## Overview

Add an orchestrator agent and dynamic dev pool to MyDevTeam. The orchestrator autonomously decomposes high-level tasks into sub-tasks and assigns them to N configurable dev agents. Users monitor via dashboard and can edit dev configs (model, directory) and task descriptions at any time, including mid-flight.

## Key Decisions

- **Hub-and-spoke communication** — devs talk through the orchestrator, never directly to each other. Orchestrator relays context between devs.
- **Orchestrator is a new agent type** — runs as a subprocess like other agents, claims `orchestrator` tasks, uses the daemon API to manage sub-tasks.
- **API-only interface** — no chat UI for now. User submits tasks via API, orchestrator decomposes autonomously.
- **Autonomous decomposition** — orchestrator breaks down tasks and assigns without confirmation, but dev assignments and configs are always visible and editable.
- **Hot-swap models** — dev agents read their config before each LLM call. Model changes take effect immediately; directory/repo changes take effect between tasks.
- **Pull-based task edit notification** — revision counter on tasks, agents poll for changes.
- **Two-layer retry** — BaseAgent retries transient errors (network, timeouts); orchestrator retries logical failures (bad code, wrong approach) across task attempts.

## Data Model Changes

### New: TaskType.ORCHESTRATOR

Added to the `TaskType` enum. High-level tasks submitted by users have type `orchestrator`.

### Task Model Additions

```python
class Task(BaseModel):
    # ... existing fields ...
    parent_id: UUID | None = None   # orchestrator task that spawned this
    revision: int = 0               # incremented on each edit, agents track last seen
```

- `parent_id` makes the orchestrator→dev relationship first-class (replaces implicit `params["parent_id"]`)
- `revision` enables pull-based mid-flight notification without new communication channels

### New: DevSlot Model

```python
class DevSlot(BaseModel):
    id: str
    model: str              # e.g. "qwen3:8b"
    provider: str           # e.g. "ollama"
    endpoint: str           # e.g. "http://localhost:11434"
    working_dir: str        # repo path or workspace path
    repo_url: str           # git remote
```

Stored in SQLite, editable via API. Dev agents read their slot config before each LLM call.

## Task Editing

### Endpoint: PATCH /api/task/edit

Accepts:
- `task_id: UUID`
- `description: str | None` — new description
- `params: dict | None` — merged into existing params
- `priority: int | None` — new priority

Rules:
- Pending tasks: all fields editable freely
- In-progress tasks: all fields editable, agent notified via revision bump
- Completed/failed/cancelled: returns 400

### Mid-Flight Notification

When an in-progress task is edited, `revision` increments. The dev agent tracks `last_seen_revision`. Before each LLM call:

1. Agent fetches current task state
2. If `task.revision > last_seen_revision`: re-read description/params, inject updated context into next LLM prompt
3. Update `last_seen_revision`

What the agent does with edits:
- Description changed: includes both old and new in next LLM prompt with instruction to adjust approach
- Params changed: uses new params going forward
- Priority changed: no agent impact, only affects queue ordering

## Orchestrator Agent

### Location: `agents/orchestrator/agent.py`

Follows the same `BaseAgent` pattern — subprocess, claim loop, heartbeat. Claims tasks of type `orchestrator`.

### Flow

1. User submits task via API → type `orchestrator`
2. Orchestrator claims it
3. Orchestrator calls LLM with: task description + current dev pool state (how many devs, what they're working on, their models)
4. LLM returns decomposition: list of sub-tasks with suggested dev assignments
5. Orchestrator creates `dev` tasks via daemon API, setting `parent_id` to the orchestrator task
6. Orchestrator moves to `in_progress`, monitors children in a polling loop (every 5-10s)
7. As dev tasks complete/fail, orchestrator evaluates progress — may create follow-ups, reassign, or mark itself complete
8. On dev failure, orchestrator decides: retry same dev, reassign to different dev, simplify task, or escalate

### Hub-and-Spoke Communication

The orchestrator reads all child tasks' outputs. When dev-1 completes and its output mentions something relevant to dev-2, the orchestrator includes that context in dev-2's task params. The orchestrator is the only agent that sees all devs' outputs.

### Failure Handling

Two layers:

| Layer | Handler | Scope |
|---|---|---|
| Transient errors (network, timeout) | BaseAgent retry (max 2, exponential backoff) | Within single task attempt |
| Logical failures (bad code, wrong approach) | Orchestrator | Across task attempts |

Orchestrator failure decisions:
1. **Retry same dev** — new task with error context
2. **Reassign** — different dev, possibly stronger model
3. **Simplify** — break into smaller pieces
4. **Escalate** — mark orchestrator task as failed after 3 retries

Retry count tracked in `params["retry_count"]`.

## Dev Pool

### DevPool Component

New component in `AgentManager` managing mutable dev slots.

### How Devs Are Spawned

- On startup: config defines initial dev slots
- At runtime: orchestrator or admin creates new slots via API
- On deletion: agent exits cleanly after current task

### Hot-Swap Mechanism

Two check points in the dev agent:

1. **Before each LLM call** — reads DevSlot from API, uses current model/provider/endpoint
2. **Between tasks** — checks if slot still exists; if deleted, exits

Effect timing:
- Model/provider/endpoint change → next LLM call (mid-task)
- working_dir/repo_url change → next task (between tasks)

### API Endpoints

| Endpoint | Description |
|---|---|
| `POST /api/dev/create` | Create new dev slot, spawns agent |
| `GET /api/dev/list` | List all dev slots with current state |
| `PATCH /api/dev/{id}` | Edit model, provider, endpoint, working_dir, repo_url |
| `DELETE /api/dev/{id}` | Remove slot, agent exits after current task |

## Backward Compatibility

- Legacy config with top-level `agents:` still works unchanged
- Orchestrator + dev pool is opt-in
- If no orchestrator configured, tasks go directly to dev agents as before
- Review → QA → deploy pipeline unchanged
- NATS sync, webhooks, WebSocket work with new task types automatically

## What Changes vs What's New

| Component | Status |
|---|---|
| `TaskType.ORCHESTRATOR` | New enum value |
| `Task.parent_id`, `Task.revision` | New fields |
| `DevSlot` model + SQLite table | New |
| `DevPool` in AgentManager | New |
| Orchestrator agent | New (`agents/orchestrator/agent.py`) |
| `PATCH /api/task/edit` | New endpoint |
| `POST/GET/PATCH/DELETE /api/dev/*` | New endpoints |
| BaseAgent config hot-read | Small change to `base/agent.py` |
| Review → QA → Deploy pipeline | Unchanged |
| NATS, webhooks, WebSocket | Unchanged |

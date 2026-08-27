# Architecture

## Overview

MyDevTeam is a daemon that orchestrates AI agents to perform software development tasks. It's a pure Python system with two main components:

1. **Daemon** — FastAPI HTTP server + SQLite store + agent process manager
2. **Agents** — Python subprocesses that claim and execute tasks via the daemon API

```
User
  │
  ▼
┌─────────────────────────────────────────┐
│             FastAPI Daemon              │
│                                         │
│  HTTP API ◄──► TaskStore (SQLite)       │
│                  │  ├── DevSlot table   │
│  AgentManager ───┤                      │
│   ├── DevPool    │                      │
│   │              ▼                      │
│   │   NATS Syncer (optional, multi-node)│
│   │                                     │
└───┼─────────────────────────────────────┘
    │
    ├── Orchestrator Agent (claims orchestrator tasks, decomposes, monitors)
    │     └── hub-and-spoke: relays context between devs
    │
    ├── Dev Agent 1 ─┐
    ├── Dev Agent 2  ├── DevPool (dynamic, hot-swappable config)
    ├── Dev Agent N ─┘
    │
    ├── Review Agent
    ├── QA Agent
    └── Deploy Agent
```

## Key Design Decisions

### Agents are subprocesses, not threads
Each agent runs in its own Python process, spawned by `AgentManager`. This gives us:
- Process isolation (one crash doesn't take down others)
- Independent resource limits (`RLIMIT_AS`, `RLIMIT_CPU` via `preexec_fn`)
- Different LLM configs per agent type

### SQLite with WAL mode
- No external database dependency
- Concurrent reads are safe, writes are serialized
- Stored at `{workspace_dir}/devteam.db`

### NATS is optional
- Single-node works without NATS (syncer becomes a no-op)
- Multi-node uses NATS JetStream with exactly-once delivery
- Subscribe uses explicit ack, nak+redelivery, deduplication cache (30s TTL), max_deliver=5

### Human gate before deploy
- QA completion does NOT auto-deploy
- Explicit approval via `POST /api/deploy/approve` required
- This creates a deploy task that the deploy agent picks up

### litellm for LLM abstraction
- One interface for ollama, anthropic, openai, etc.
- Primary + fallback provider per agent
- Configurable timeout per agent type

### Hub-and-spoke orchestration
- Orchestrator agent decomposes high-level tasks into dev sub-tasks
- Devs never communicate directly — orchestrator relays context between them
- Full audit trail: all cross-dev communication is visible as task params
- Two-layer retry: BaseAgent retries transient errors, orchestrator retries logical failures

### Hot-swappable dev config
- Dev agents read their `DevSlot` config before each LLM call
- Model/provider changes take effect mid-task on next LLM call
- Directory/repo changes take effect between tasks
- No agent restart needed for config changes

## Data Flow

### Task Pipeline
```
orchestrator → dev → review → qa → [human approval] → deploy
                 ↑         │
                 └─────────┘ (if changes_requested → new dev task)
```

The orchestrator claims high-level tasks and decomposes them into dev sub-tasks (linked via `parent_id`). From there, the existing chaining handles review → QA → deploy.

Chaining is handled in `server.py:_handle_task_completion()`. Each completed task automatically creates the next stage task with params carried forward.

### Orchestrator Flow
```
User submits orchestrator task
  → Orchestrator claims it
  → LLM decomposes into sub-tasks
  → Creates dev tasks (parent_id = orchestrator task)
  → Monitors children (polls every 5-10s)
  → Relays context between devs (hub-and-spoke)
  → On child failure: retry/reassign/simplify/escalate
  → When all children complete: mark orchestrator task complete
```

### Task Editing (Mid-Flight)
```
User edits task via PATCH /api/task/edit
  → task.revision increments
  → Dev agent checks revision before each LLM call
  → If revision changed: re-reads description/params
  → Injects updated context into next LLM prompt
```

### Agent Claim Loop
```
Agent starts → polls POST /api/task/claim every 2-60s (exponential backoff)
  → claims task matching its type + project
  → sets status to in_progress
  → executes (LLM calls, git ops)
  → sets status to completed/failed with detail string
```

### Auth Model
```
User (has api_key) → owns Projects → contain Tasks
                                        ↑
                              Agents claim by type + project_id
```

API key is passed via `X-Api-Key` header. The gate is **fail-closed**: missing or invalid key returns 401, never falls back to anonymous. Admin endpoints additionally require `is_admin=True` on the resolved user.

**Admin resolution order** (first match wins):
1. `MYDEVTEAM_API_KEY` env var matches the presented key → synthetic admin user (not DB-resident).
2. `MYDEVTEAM_ADMIN_EMAILS` (comma-separated) matches the resolved user's email → in-memory promotion (DB unchanged).
3. `UserRow.is_admin = True` in SQLite.

**Per-user additional keys**: `/api/key/{create,list,delete}` issues labeled keys stored as sha256 hashes in the `api_keys` table. The primary `UserRow.api_key` is kept plaintext for backward compatibility; future work hashes that too.

Agent-internal endpoints (claim, status, heartbeat, git) use a per-boot `X-Agent-Key` shared secret instead of user keys — they run on the same host as the daemon.

### Runtime environment overrides

- `DEVTEAM_API_ADDRESS` — overrides `api.address` from config (e.g., `0.0.0.0:4223` for Docker).
- `MYDEVTEAM_API_KEY` — overrides `api.admin_api_key` from config.
- `MYDEVTEAM_ADMIN_EMAILS` — auto-promote matching emails to admin at request time.
- `MYDEVTEAM_ALLOWED_ORIGINS` — extra CORS origins unioned with dev defaults (`http://localhost:5173`, `http://127.0.0.1:5173`).

## Directory Layout

```
daemon/
  main.py              — Entry point, wires everything together
  config.py            — YAML → pydantic Config
  api/server.py        — All HTTP + WebSocket endpoints
  api/webhooks.py      — Webhook dispatcher
  tasks/models.py      — User, Project, Task, DevSlot, state machine
  tasks/store.py       — SQLAlchemy ORM (SQLite)
  agents/manager.py    — AgentManager + DevPool: spawn/monitor/restart/hot-swap
  nats/sync.py         — NATS JetStream multi-node sync
  git/workspace.py     — Git clone/branch/commit/push

agents/
  base/agent.py        — BaseAgent: heartbeat, claim loop, retry backoff, config hot-read
  base/comms.py        — HTTP client for daemon API
  base/llm.py          — litellm wrapper (primary + fallback)
  orchestrator/agent.py — Task decomposition, dev coordination, hub-and-spoke
  dev/agent.py         — Code generation
  pr_manager/agent.py  — Code review (type "review" in config)
  qa/agent.py          — Test running + LLM test generation
  deploy/agent.py      — Docker build + compose deploy

Dockerfile             — Multi-stage Python 3.12-slim, uv-based, exposes 4223
.dockerignore          — Standard Python + git ignore patterns
```

## Multi-Node

Each server runs its own daemon + agents. NATS JetStream syncs task state:

```
Server A (GPU)              Server B (CPU)
┌─────────────────┐        ┌─────────────────┐
│ Dev (qwen3:32b) │◄──────►│ Review (claude)  │
│ QA  (qwen3:8b)  │  NATS  │ Deploy (qwen3:8b)│
└─────────────────┘        └─────────────────┘
```

Events propagate passively — each node applies remote events to its local store independently via `_handle_event`. Events are NOT forwarded (no cascading).

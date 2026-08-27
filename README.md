# MyDevTeam

Autonomous agentic dev team — 5 AI agents (orchestrator, dev, review, QA, deploy) collaborate on your repos using local LLMs via ollama.

```
Task submitted ──► Orchestrator ──► Dev Agent(s) ──► Review ──► QA ──► [human gate] ──► Deploy
                   (decomposes)     (writes code)    (review)   (tests)                 (docker)
```

The orchestrator breaks high-level tasks into sub-tasks and coordinates N dev agents. Each dev has configurable model, directory, and hot-swappable LLM config. Tasks are editable mid-flight.

## Architecture

```
daemon/                          agents/
┌──────────────────────────┐     ┌─────────────────────────┐
│ FastAPI HTTP server      │     │ BaseAgent               │
│ SQLAlchemy + SQLite(WAL) │◄────│   heartbeat + claim loop│
│ AgentManager (per-proj)  │     │   litellm (ollama/cloud)│
│ NATS sync (optional)     │     │   git ops               │
└──────────────────────────┘     ├─────────────────────────┤
                                 │ OrchestratorAgent       │
                                 │              – decompose│
                                 │ DevAgent     – code gen │
                                 │ PRManager    – review   │
                                 │ QAAgent      – tests    │
                                 │ DeployAgent  – docker   │
                                 └─────────────────────────┘
```

**This repo is the daemon (API server + agents).** Clients are separate projects:
- **CLI** — separate project, talks to the API
- **Web UI** — separate project, talks to the API

**Auth:** API key via `X-Api-Key` header — fail-closed (missing/invalid key returns 401, never falls back to anonymous). Admin endpoints require `is_admin=True` on the resolved user. Three admin sources, checked in order: (1) `MYDEVTEAM_API_KEY` env var matches the presented key → synthetic admin (not persisted); (2) `MYDEVTEAM_ADMIN_EMAILS` (comma-separated) matches the user's email → in-memory promotion; (3) `is_admin=True` on the `UserRow` in SQLite. Per-user additional API keys (sha256-hashed) live in the `api_keys` table.
**Agents** are subprocesses scoped to a project via `DEVTEAM_PROJECT_ID` env var.

## Quick Start

```bash
# Install
python -m venv .venv && source .venv/bin/activate
pip install -e .

# Ensure ollama is running with a model
ollama pull qwen3:8b

# Start daemon
python -m daemon.main --config config/local-test.yaml

# In another terminal — admin creates a user, then submit via API
curl -X POST localhost:4223/api/admin/user/create \
  -H "X-Api-Key: mydevteam-local-admin-key" \
  -H "Content-Type: application/json" \
  -d '{"name":"you","email":"you@example.com"}'
# Use returned api_key:
curl -X POST localhost:4223/api/project/create \
  -H "X-Api-Key: <key>" -H "Content-Type: application/json" \
  -d '{"name":"my-app","repo_url":"file:///path/to/repo"}'
curl -X POST localhost:4223/api/task/create \
  -H "X-Api-Key: <key>" -H "Content-Type: application/json" \
  -d '{"project_id":"<id>","description":"Create hello.py that prints hello"}'
```

## Codebase

```
daemon/
  api/server.py        — FastAPI app, all HTTP + WebSocket endpoints
  api/test_server.py   — 60 API auth + pipeline tests
  tasks/models.py      — User, Project, Task, state machine
  tasks/store.py       — SQLAlchemy ORM (UserRow, ProjectRow, TaskRow, HistoryRow)
  agents/manager.py    — AgentManager: spawn/monitor/restart subprocesses per-project
  nats/sync.py         — Optional NATS JetStream multi-node sync
  git/workspace.py     — Git clone/branch/commit/push via subprocess
  config.py            — YAML config parsing (supports legacy + project-based)
  main.py              — Entry point (starts daemon server)

agents/
  base/agent.py        — BaseAgent: heartbeat, claim loop, git ops, project scoping
  base/comms.py        — DaemonClient: HTTP client (auto-detects TCP vs Unix socket)
  base/llm.py          — LLMClient: litellm wrapper with primary/fallback
  orchestrator/agent.py — Decomposes high-level tasks into dev sub-tasks via hub-and-spoke; wired in `daemon/agents/manager.py` MODULE_MAP and accepted as task `type` in `daemon/api/server.py`
  dev/agent.py         — LLM-driven code generation
  pr_manager/agent.py  — Code review
  qa/agent.py          — Test runner + LLM test generation
  deploy/agent.py      — Docker build + compose deploy

config/
  local-test.yaml          — Single-node config (legacy top-level agents format)
  docker.yaml              — Container config: binds 0.0.0.0, ollama on host.docker.internal, no baked admin key
  example.yaml             — Multi-node example (legacy format)
  example-projects.yaml    — Multi-project config format
```

## Task Pipeline

```
dev → review → qa → [human gate] → deploy
```

- Dev completes → review task auto-created
- Review approves → QA task auto-created
- Review requests changes → new dev task created (loop)
- QA completes → **waits for human approval**
- Human approves → deploy task created

### State Machine

```
pending → assigned → in_progress → completed / failed / blocked / cancelled
blocked → pending (retry)    failed → pending (retry)
needs_changes → pending      completed → needs_changes (reopen)
cancelled → (terminal)
```

## API Endpoints

### Admin (requires `X-Api-Key` header + admin flag)
| Endpoint | Description |
|----------|-------------|
| `POST /api/admin/user/create` | Create user |
| `POST /api/admin/user/list` | List all users |
| `POST /api/admin/user/delete` | Delete user |
| `POST /api/dev/create` | Create dev slot (spawns agent) |
| `GET /api/dev/list` | List all dev slots |
| `PATCH /api/dev/{id}` | Update dev slot config (hot-swap model) |
| `DELETE /api/dev/{id}` | Remove dev slot |

### User-authenticated (requires `X-Api-Key` header)
| Endpoint | Description |
|----------|-------------|
| `POST /api/user/me` | Get current user |
| `POST /api/key/create` | Issue a new API key (returns plaintext once) |
| `POST /api/key/list` | List the caller's additional keys (metadata only) |
| `POST /api/key/delete` | Revoke one of the caller's additional keys |
| `POST /api/project/create` | Create project |
| `POST /api/project/list` | List user's projects |
| `POST /api/project/delete` | Delete project + tasks |
| `POST /api/task/create` | Submit task (`type`: `dev` or `orchestrator`) |
| `POST /api/task/get` | Get task details (includes `parent_id`, `revision`) |
| `POST /api/task/list` | List tasks (filterable) |
| `PATCH /api/task/edit` | Edit task description/params/priority (bumps revision) |
| `POST /api/task/cancel` | Cancel a task |
| `POST /api/task/retry` | Retry a failed task |
| `PATCH /api/task/priority` | Update task priority |
| `POST /api/task/delete` | Delete a task |
| `POST /api/deploy/approve` | Approve deploy after QA |
| `POST /api/webhook/create` | Register webhook |
| `POST /api/webhook/list` | List webhooks |
| `POST /api/webhook/delete` | Delete webhook |
| `POST /api/agents/list` | List running agents |
| `POST /api/agent/logs` | Get agent log output |
| `POST /api/agent/config` | Get agent config |
| `POST /api/dashboard/stats` | Task stats by type/status |
| `POST /api/node/info` | Node uptime, agent count |
| `WS /ws/tasks` | Real-time task updates |

### Public (no auth)
| Endpoint | Description |
|----------|-------------|
| `GET /healthz` | Liveness probe for container orchestrators: `{"status":"ok","node_id":...}`. Reports that the process is up, not that agents or the LLM backend are healthy. |

### Agent-internal (no user auth)
| Endpoint | Description |
|----------|-------------|
| `POST /api/task/claim` | Agent claims next task |
| `POST /api/task/status` | Agent updates task status |
| `POST /api/task/get_internal` | Agent reads task (includes revision) |
| `POST /api/task/create_internal` | Orchestrator creates child tasks |
| `POST /api/task/list_internal` | Orchestrator lists children by parent_id |
| `POST /api/task/edit_internal` | Orchestrator edits sibling context |
| `POST /api/dev/pool_state` | Orchestrator reads dev pool |
| `POST /api/dev/slot` | Dev agent reads its slot config |
| `POST /api/heartbeat` | Agent heartbeat |
| `POST /api/git/{clone,branch,commit,push}` | Git operations |

## Configuration

### Project-based (recommended)
```yaml
cluster:
  node_id: "my-node"
  seeds: []          # NATS seeds for multi-node

api:
  address: "localhost:4223"

projects:
  - name: backend
    repo_url: "git@github.com:org/backend.git"
    agents:
      - type: dev
        count: 1
        llm:
          primary:
            provider: ollama
            model: qwen3:8b
            endpoint: http://localhost:11434
          timeout: 120s
      - type: review
        count: 1
        llm:
          primary:
            provider: ollama
            model: qwen3:8b
            endpoint: http://localhost:11434

git:
  workspace_dir: "/tmp/devteam-workspaces"
```

### Legacy (top-level agents, auto-wrapped into "default" project)
```yaml
agents:
  - type: dev
    count: 1
    llm: ...
```

### LLM providers
```yaml
# Local (ollama)
llm:
  primary:
    provider: ollama
    model: qwen3:8b
    endpoint: http://localhost:11434

# Cloud with fallback
llm:
  primary:
    provider: anthropic
    model: claude-sonnet-4-6
  fallback:
    provider: openai
    model: gpt-4o
  timeout: 120s
```

Cloud providers need env vars: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`.

### Runtime environment overrides

| Env var | Default | Purpose |
|---------|---------|---------|
| `DEVTEAM_API_ADDRESS` | `api.address` from config | Advertised address — what agent subprocesses dial (`DEVTEAM_SOCKET`). The bind falls back to it, so setting `0.0.0.0:4223` still listens on all interfaces; note that agents then dial `0.0.0.0:4223` too, which works on Linux but is imprecise. Prefer the pair below. |
| `DEVTEAM_BIND_ADDRESS` | `api.bind_address`, else the advertised address | Interface uvicorn listens on, independent of what agents dial. The precise way to expose a container: bind `0.0.0.0:4223` while agents keep using `localhost:4223`. |
| `MYDEVTEAM_API_KEY` | `api.admin_api_key` from config | Platform admin key. Requests presenting this in `X-Api-Key` are treated as a synthetic admin user (not DB-resident). |
| `MYDEVTEAM_ADMIN_EMAILS` | (unset) | Comma-separated emails auto-promoted to admin at request time (in-memory; not persisted). |
| `MYDEVTEAM_ALLOWED_ORIGINS` | (unset) | Comma-separated extra CORS origins, unioned with `http://localhost:5173` / `http://127.0.0.1:5173`. |

### Containerized runs

A `Dockerfile` (multi-stage, `uv`-based, Python 3.12-slim) ships in the repo root. Build and run directly, or use the parent monorepo's `docker-compose.yml`:

```bash
docker build -t mydevteam:dev .
docker run -p 4223:4223 \
  -e MYDEVTEAM_API_KEY=local-admin-key \
  mydevteam:dev
```

The image's default command runs `config/docker.yaml`, which already sets
`bind_address: 0.0.0.0:4223` (with `address: localhost:4223` for the agents),
points the agents' LLM endpoint at `host.docker.internal`, and carries no admin
key — so `MYDEVTEAM_API_KEY` is required or the daemon fails closed and exits.
`config/local-test.yaml` is for native dev and will *not* be reachable from
outside a container.

`GET /healthz` is unauthenticated and reports process liveness plus `node_id`,
for container health probes. Every other route is POST behind auth.

## Multi-Server Setup

Each server runs its own daemon + agents. NATS JetStream syncs task state across nodes.

```
┌─── Server A (GPU) ────────┐     ┌─── Server B (CPU) ────────┐
│ Dev Agent (qwen3:32b)     │     │ Review Agent (claude)     │
│ QA Agent  (qwen3:8b)      │◄───►│ Deploy Agent (qwen3:8b)   │
│        NATS cluster        │     │        NATS cluster        │
└───────────────────────────┘     └───────────────────────────┘
```

```yaml
# server-a.yaml
cluster:
  node_id: "server-a"
  seeds: ["server-b:4222"]
```

See `config/example.yaml` and `config/example-projects.yaml` for full examples.

## Tests

```bash
source .venv/bin/activate
python -m pytest daemon/api/test_server.py -q    # 60 API tests
```

Test coverage:
- **API tests** (60): Auth (fail-closed admin gate, env-overridable admin key, email auto-promotion), user isolation, project access control, additional API key management, task CRUD, full pipeline chain (dev→review→qa→deploy), dashboard stats, agent config/logs
- **Agent tests**: BaseAgent, DevAgent, PRManager, QA, Deploy

## Roadmap

All items from the original roadmap are complete. This repo is the daemon/API server only — frontend and CLI are separate projects.

### Done
- [x] Consumer groups for NATS (exactly-once delivery: explicit ack, nak+redelivery, deduplication cache, max_deliver=5)
- [x] Agent refinement (smarter retry backoff, better PR manager with dynamic default branch detection and GitHub API retry backoff)
- [x] Fix orphaned Go binary and broken Makefile targets
- [x] Fix duplicate `list_statuses` in `daemon/agents/manager.py`
- [x] Add missing `httpx` and `gitpython` to `pyproject.toml` dependencies
- [x] Pure Python rewrite (was Go + Python)
- [x] User + Project models with API key auth
- [x] Per-project agent spawning with `DEVTEAM_PROJECT_ID`
- [x] Task pipeline chaining (dev→review→qa→human gate→deploy)
- [x] Real ollama e2e test (qwen3:8b, full pipeline)
- [x] Go code removal
- [x] CLI split out (separate project)
- [x] Multi-project config format
- [x] Config-created projects owned by admin
- [x] Structured agent logging (all 4 agents + BaseAgent)
- [x] OpenAPI spec v2.3.0 (admin endpoints, webhooks, priority)
- [x] Orchestrator agent (decomposition, hub-and-spoke, child monitoring, two-layer retry)
- [x] Multi-Dev Architecture / DevPool (DevSlot model, hot-swap, CRUD API, dynamic scaling)
- [x] Task editing (PATCH /api/task/edit, parent_id, revision counter, mid-flight re-read)
- [x] Agent-internal endpoint auth (X-Agent-Key shared secret per boot)
- [x] Security audit fixes (path traversal, atomic claim, unbounded context cap, orchestrator fail-fast)
- [x] Webhook notifications (task.created, status_changed, completed, failed, approved)
- [x] QA + Deploy agents with real ollama (full pipeline verified)
- [x] Web UI (separate project)
- [x] Agent resource limits (RLIMIT_AS, RLIMIT_CPU per subprocess)
- [x] Task priority + queuing (priority field, PATCH /api/task/priority)
- [x] Multi-node NATS sync (full e2e 2-node test verified)
- [x] `/api/agent/logs` returns structured log data for frontend

## Key Design Decisions

- **Agents are subprocesses, not threads** — isolation, independent crashes, different LLM configs per agent.
- **SQLite with WAL mode** — simple, no external DB dependency, concurrent reads.
- **NATS is optional** — single-node works without it, graceful no-op.
- **Human gate before deploy** — QA completion does NOT auto-deploy. Explicit approval required.
- **litellm for LLM abstraction** — one interface for ollama, anthropic, openai, etc.
- **Config type `review` maps to `agents.pr_manager.agent`** — via `MODULE_MAP` in manager.py.

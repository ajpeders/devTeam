# How-To Guide

## Start the Daemon

```bash
source .venv/bin/activate
ollama pull qwen3:8b  # ensure model is available
python -m daemon.main --config config/local-test.yaml
```

The daemon starts on `localhost:4223` by default. Admin API key is set in the config file.

### Bind to all interfaces (Docker / reverse proxy)

```bash
export DEVTEAM_API_ADDRESS=0.0.0.0:4223
python -m daemon.main --config config/local-test.yaml
```

`DEVTEAM_API_ADDRESS` takes precedence over the config file's `api.address`. Required when running under Docker so the container exposes the port outward.

### Configure additional CORS origins

By default only `http://localhost:5173` / `http://127.0.0.1:5173` are allowed. To add more:

```bash
export MYDEVTEAM_ALLOWED_ORIGINS="https://devteam.example.com,https://staging.example.com"
```

Comma-separated, case-sensitive, unioned with the defaults.

### Promote users to admin by email

Instead of setting `is_admin=True` in the DB, you can auto-promote at request time:

```bash
export MYDEVTEAM_ADMIN_EMAILS="alex@example.com,ops@example.com"
```

The promotion is in-memory only — the DB row is untouched. Useful for short-lived admin sessions or bootstrap.

### Run in Docker

```bash
docker build -t mydevteam:dev -f Dockerfile .
docker run -p 4223:4223 \
  -e DEVTEAM_API_ADDRESS=0.0.0.0:4223 \
  -e MYDEVTEAM_API_KEY=local-admin-key \
  -e MYDEVTEAM_ALLOWED_ORIGINS="http://myweb:5173" \
  mydevteam:dev
```

Or use the parent monorepo's `docker-compose.yml` (`docker compose up --build`).

## Create a User

Users are created by the admin:

```bash
curl -X POST localhost:4223/api/admin/user/create \
  -H "X-Api-Key: mydevteam-local-admin-key" \
  -H "Content-Type: application/json" \
  -d '{"name": "alex", "email": "alex@example.com"}'
```

Response includes the new user's `api_key`. Save it — it's the only time it's shown.

## Issue Additional API Keys

Each user starts with one primary `api_key` (the one returned at user-create time). For per-environment isolation (CI vs. local vs. prod) you can issue additional keys without revoking the primary:

```bash
# Create a labeled key (label is free-text; appears in /api/key/list)
curl -X POST localhost:4223/api/key/create \
  -H "X-Api-Key: <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"label": "ci"}'
# Response: { "id": "...", "label": "ci", "key": "<plaintext-shown-once>", "created_at": "..." }

# List your additional keys (the primary key is intentionally excluded)
curl -X POST localhost:4223/api/key/list \
  -H "X-Api-Key: <your-api-key>"

# Revoke an additional key
curl -X POST localhost:4223/api/key/delete \
  -H "X-Api-Key: <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"key_id": "<key-uuid-from-list>"}'
```

Storage: additional keys are stored as `sha256` hashes in the `api_keys` table — only the metadata (id, label, created_at) is retrievable after issue. The primary `UserRow.api_key` remains plaintext for backward compatibility; future work hashes that too.

## Create a Project

```bash
curl -X POST localhost:4223/api/project/create \
  -H "X-Api-Key: <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"name": "my-app", "repo_url": "file:///path/to/repo"}'
```

## Submit a Task

```bash
curl -X POST localhost:4223/api/task/create \
  -H "X-Api-Key: <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"project_id": "<project-id>", "description": "Add a login page", "priority": 5}'
```

Higher priority = processed first. The task enters the pipeline: dev → review → QA → deploy.

## Monitor Tasks

```bash
# List all tasks for a project
curl -X POST localhost:4223/api/task/list \
  -H "X-Api-Key: <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"project_id": "<project-id>"}'

# Get a specific task
curl -X POST localhost:4223/api/task/get \
  -H "X-Api-Key: <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"task_id": "<task-id>"}'
```

Or connect to the WebSocket at `ws://localhost:4223/ws/tasks` for real-time updates.

## Approve a Deploy

After QA completes, deployment requires human approval:

```bash
curl -X POST localhost:4223/api/deploy/approve \
  -H "X-Api-Key: <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"task_id": "<qa-task-id>"}'
```

## View Agent Logs

```bash
curl -X POST localhost:4223/api/agent/logs \
  -H "X-Api-Key: <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "<agent-id>", "tail_lines": 50}'
```

## Run Tests

```bash
source .venv/bin/activate
python -m pytest daemon/api/test_server.py -x -q   # 60 API tests
python -m pytest agents/ -q                          # agent tests
python tests/e2e_multi_node.py                      # 2-node NATS test
```

## Configure Multiple Projects

Use the project-based config format instead of legacy top-level agents:

```yaml
projects:
  - name: backend
    repo_url: "git@github.com:org/backend.git"
    agents:
      - type: dev
        count: 2
        llm:
          primary: { provider: ollama, model: qwen3:8b, endpoint: http://localhost:11434 }
      - type: review
        count: 1
        llm:
          primary: { provider: anthropic, model: claude-sonnet-4-6 }

  - name: frontend
    repo_url: "git@github.com:org/frontend.git"
    agents:
      - type: dev
        count: 1
        llm:
          primary: { provider: ollama, model: qwen3:8b, endpoint: http://localhost:11434 }
```

Each project gets its own `AgentManager` and isolated agent subprocesses.

## Set Up Multi-Node with NATS

1. Install and start NATS with JetStream enabled on each node
2. Configure `cluster.seeds` in each node's config:

```yaml
# node-a.yaml
cluster:
  node_id: "node-a"
  seeds: ["nats://node-b:4222"]
```

3. Start the daemon on each node — they sync automatically

## Set Resource Limits on Agents

```yaml
agents:
  - type: dev
    count: 1
    max_memory_mb: 2048    # RLIMIT_AS
    max_cpu_percent: 80    # RLIMIT_CPU
    llm: ...
```

Limits are applied via `preexec_fn` in the spawned subprocess.

## Set Up Webhooks

```bash
curl -X POST localhost:4223/api/webhook/create \
  -H "X-Api-Key: <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"project_id": "<project-id>", "url": "https://hooks.example.com/devteam", "events": ["task.created", "task.completed", "task.failed"]}'
```

Available events: `task.created`, `task.status_changed`, `task.completed`, `task.failed`, `task.approved`

## Use the Orchestrator

Submit a high-level task — the orchestrator decomposes it and assigns to dev agents:

```bash
curl -X POST localhost:4223/api/task/create \
  -H "X-Api-Key: <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"project_id": "<project-id>", "description": "Build a user authentication system with login, signup, and password reset", "type": "orchestrator"}'
```

The orchestrator breaks this into sub-tasks, assigns them to available devs, and monitors progress. Each dev sub-task flows through the normal pipeline (dev → review → QA → deploy).

## Manage Dev Agents

### List dev slots
```bash
curl -s localhost:4223/api/dev/list -H "X-Api-Key: <key>" | jq
```

### Create a new dev
```bash
curl -X POST localhost:4223/api/dev/create \
  -H "X-Api-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{"model": "qwen3:8b", "provider": "ollama", "endpoint": "http://localhost:11434", "repo_url": "file:///path/to/repo"}'
```

### Hot-swap a dev's model (takes effect on next LLM call)
```bash
curl -X PATCH localhost:4223/api/dev/<dev-id> \
  -H "X-Api-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-sonnet-4-6", "provider": "anthropic", "endpoint": ""}'
```

### Remove a dev (exits after current task)
```bash
curl -X DELETE localhost:4223/api/dev/<dev-id> -H "X-Api-Key: <key>"
```

## Edit a Task Mid-Flight

Change a task's description or params while it's pending or in-progress:

```bash
curl -X PATCH localhost:4223/api/task/edit \
  -H "X-Api-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{"task_id": "<task-id>", "description": "Updated requirements: also add OAuth2 support"}'
```

If the task is in-progress, the assigned dev picks up the change before its next LLM call.

# devTeam — Autonomous Agentic Development Team

## Overview

An autonomous software development team composed of specialized AI agents (Dev, PR Manager, QA, Deploy) that collaborate across multiple servers to take work from task description to deployed code. Agents communicate via NATS messaging, persist state in SQLite, use git as the shared source of truth, and can each run different LLMs (local or cloud).

## Architecture

### Peer-to-Peer Node Topology

Each server runs a **Node Daemon** (Go binary) that:

- Embeds a NATS server, clustered with other nodes via seed list
- Manages local Python agent processes (spawn, monitor, restart)
- Maintains a SQLite task store, synced across the cluster via NATS
- Handles git workspace operations (clone, branch, push/pull)
- Exposes a Unix socket gRPC API for local agents

There is no central orchestrator. Any node can accept tasks. NATS distributes work to the appropriate agent regardless of which server it runs on.

```
┌─────────── Server A ───────────┐     ┌─────────── Server B ───────────┐
│                                │     │                                │
│  ┌──────────────────────────┐  │     │  ┌──────────────────────────┐  │
│  │   Node Daemon (Go)       │  │     │  │   Node Daemon (Go)       │  │
│  │  ┌────────┐ ┌─────────┐  │  │     │  │  ┌────────┐ ┌─────────┐  │  │
│  │  │Embedded│ │  Agent   │  │◄─NATS──►│  │Embedded│ │  Agent   │  │  │
│  │  │ NATS   │ │ Manager  │  │ cluster │  │ NATS   │ │ Manager  │  │  │
│  │  └────────┘ └─────────┘  │  │     │  └────────┘ └─────────┘  │  │
│  │  ┌─────────────────────┐  │  │     │  ┌─────────────────────┐  │  │
│  │  │  Task Store (SQLite) │  │  │     │  │  Task Store (SQLite) │  │  │
│  │  └─────────────────────┘  │  │     │  └─────────────────────┘  │  │
│  └──────────────────────────┘  │     │  └──────────────────────┘  │
│                                │     │                                │
│  ┌─────────┐ ┌─────────┐      │     │  ┌─────────┐ ┌─────────┐      │
│  │Dev Agent│ │QA Agent │      │     │  │PR Mgr   │ │Deploy   │      │
│  │(Python) │ │(Python) │      │     │  │Agent    │ │Agent    │      │
│  └─────────┘ └─────────┘      │     │  │(Python) │ │(Python) │      │
└────────────────────────────────┘     │  └─────────┘ └─────────┘      │
                                       └────────────────────────────────┘
```

### Distributed Agent Placement

Any agent can run on any server. Agents don't know or care where other agents are — NATS handles routing.

Example multi-server layout:

| Server | Hardware | Agents | LLMs |
|--------|----------|--------|------|
| Server A | RTX 4090 GPU | Dev Agent x2, QA Agent | ollama: deepseek-coder-v3, qwen2.5-coder |
| Server B | CPU-only VPS | PR Manager, Deploy Agent | Claude API (cloud) |
| Server C | Laptop | Dev Agent | ollama: codellama |

## Agent Roles

### Dev Agent

- **Trigger:** New task assigned (issue, spec, or natural language description)
- **Actions:** Clones repo, creates feature branch, writes/modifies code, commits, pushes branch
- **Output:** Git branch with commits pushed to remote
- **LLM needs:** High — code generation is the most demanding task. Benefits from large local models or frontier cloud APIs.

### PR Manager Agent

- **Trigger:** Dev Agent pushes a branch
- **Actions:** Creates pull request, reviews the code (logic, style, security, correctness), writes review comments. If issues found, requests changes and sends task back to Dev Agent. If satisfied, approves and notifies human for final approval.
- **Output:** PR with review comments, approval or change requests
- **LLM needs:** High — code review requires strong reasoning. Good candidate for frontier cloud APIs.

### QA Agent

- **Trigger:** PR Manager approves the code
- **Actions:** Runs existing test suite against the PR branch, writes new tests for changed code, runs new tests, reports results
- **Output:** Test results + new test files committed to PR branch
- **LLM needs:** Medium — test generation needs code understanding but is more structured than open-ended development.

### Deploy Agent

- **Trigger:** Human approves PR + QA passes
- **Actions:** Merges PR, builds Docker image, pushes to registry, deploys container to target server
- **Output:** Running container on target environment
- **LLM needs:** Low — mostly executes a defined pipeline. Could use an LLM for interpreting error logs or making rollback decisions.

## Task Pipeline

```
[Input]
  │
  ▼
┌──────┐    ┌──────────────┐    ┌────────┐    ┌────────┐
│ Dev  │───►│  PR Manager  │───►│  QA    │───►│ Deploy │
│Agent │    │ (code review │    │ Agent  │    │ Agent  │
└──────┘    │  + PR mgmt)  │    └────────┘    └────────┘
            └──────────────┘
                 │    ▲              │              │
                 │    │              │              ⊙
                 │    └──────────────┘         Human approves
                 │     Request changes?         deployment
                 │     Loop back to Dev
                 ⊙
           Human final
            approval
```

**Human gates (⊙):** Humans approve PRs (after PR Manager review) and deployments. Agents handle everything else autonomously.

## Data Model

### Task Object

```
Task {
  id:           uuid
  type:         "dev" | "review" | "qa" | "deploy"
  status:       "pending" | "assigned" | "in_progress" | "blocked" |
                "needs_changes" | "completed" | "failed"
  input: {
    source:     "issue" | "spec" | "natural_language"
    content:    string          # the actual task description
    repo_url:   string          # git remote
    branch:     string | null   # set once dev creates it
    pr_url:     string | null   # set once PR manager creates it
  }
  assigned_to:  agent_id | null
  node_id:      string          # which server owns this task
  parent_id:    uuid | null     # links review/qa/deploy back to original
  history: [
    { timestamp, agent_id, action, detail }
  ]
  created_at:   timestamp
  updated_at:   timestamp
}
```

### Task Chaining

```
Task #1 (type: dev)
  └─► Task #2 (type: review, parent: #1)
        ├─► Task #1 re-opened (status: needs_changes)  ← review loop
        └─► Task #3 (type: qa, parent: #1)
              └─► Task #4 (type: deploy, parent: #1)
```

## Task State Machine

Valid status transitions per task type:

```
                    ┌─────────────────────────────┐
                    ▼                             │
pending ──► assigned ──► in_progress ──► completed
                              │              │
                              ▼              ▼
                           failed    needs_changes ──► assigned (re-claim)
```

- **Dev tasks:** Can transition to `needs_changes` when PR Manager requests changes. The original dev task is re-opened (status set to `needs_changes`, then back to `assigned` when a Dev Agent re-claims it). No new task is created — the same task goes through another cycle.
- **Review/QA/Deploy tasks:** Follow the linear path. `failed` is terminal for that attempt.

## Task Assignment Protocol

Task routing uses **NATS queue groups** to prevent double-assignment:

1. Node daemon publishes task to `task.assigned.{agent-type}` with a queue group name (e.g., `workers.dev`)
2. NATS delivers to exactly one subscriber in the group — one agent on one server
3. Agent receives the message, calls daemon gRPC `ClaimTask(task_id)` which atomically sets `assigned_to` and `status: assigned` in SQLite
4. If claim fails (race condition — another agent got it first), the agent drops it. NATS will have already delivered to only one agent per queue group, so this is a safety net, not the normal path.
5. Agent publishes `task.completed.{task-id}` or `task.failed.{task-id}` when done

## Task Store Sync

Each node maintains its own SQLite database. Sync uses **event sourcing via NATS JetStream**:

1. All task mutations (create, status change, assignment) are published as ordered events to a JetStream stream (`TASKS`)
2. Every node subscribes to this stream and materializes events into its local SQLite
3. The stream is the source of truth — SQLite is a local read cache + the working copy for the owning node
4. On node restart, the node replays the stream from its last known sequence number to catch up

**Task ownership:** The `node_id` field determines which node can mutate a task. Only the owning node publishes mutation events for its tasks. Ownership transfer works as follows: when an agent on node B claims a task owned by node A, node B publishes a special `task.ownership.transfer` event (the one exception to the "only owner mutates" rule). All nodes process this event, updating `node_id` to node B. From that point forward, node B is the owner and publishes all subsequent mutations.

## Git Workspace Isolation

Each task gets its own directory under the configured workspace path:

```
/var/devteam/workspaces/
├── {task-id-1}/          # Dev Agent working on task 1
│   └── repo/             # Full clone or worktree
├── {task-id-2}/          # Dev Agent working on task 2
│   └── repo/
└── .shared-clone/        # Bare clone used as local cache for faster cloning
```

Multiple agents on the same server never share a workspace. The `.shared-clone` bare repo is used as a local reference (`git clone --reference`) to avoid re-downloading the full repo for each task.

Workspaces are cleaned up after task completion (configurable retention period for debugging).

## Failure Handling

- **Agent crash:** Daemon detects missing heartbeat (30s timeout). Task status set to `failed`. Task re-published to the queue for another agent to pick up. Max 3 automatic retries, then marked `failed` permanently and human notified.
- **QA failure (tests fail):** Task sent back to Dev via the review changes loop — same path as PR Manager requesting changes. QA Agent posts test results as context for the Dev Agent.
- **Deploy failure:** Task marked `failed`, human notified. No automatic retry for deployments — too risky.
- **Node goes down:** Other nodes continue operating. Tasks owned by the dead node remain in their last known state. When the node comes back, it replays JetStream to catch up.

## Human Gate Mechanism

**PR approval:** PR Manager creates a GitHub PR and adds a `needs-human-review` label. The PR Manager agent polls for PR approval events via GitHub API. When a human approves the PR on GitHub, the agent detects it and advances the pipeline to QA.

**Deploy approval:** After QA passes, the system waits for a CLI command:
```
devteam approve deploy {task-id}
```
A simple CLI that publishes to `deploy.requested.{task-id}` on NATS.

## Communication

### NATS Subject Structure

| Subject | Purpose |
|---------|---------|
| `task.created` | New task submitted |
| `task.assigned.{agent-type}` | Task routed to a specific agent type |
| `task.completed.{task-id}` | Agent finished work |
| `task.failed.{task-id}` | Agent encountered an error |
| `review.requested.{task-id}` | Dev done, PR Manager should review |
| `review.changes.{task-id}` | PR Manager bouncing back to Dev |
| `qa.requested.{task-id}` | PR approved, QA should run |
| `deploy.requested.{task-id}` | Human approved, deploy should run |
| `agent.heartbeat.{node-id}` | Health monitoring |

### Agent ↔ Node Daemon Protocol

Agents communicate with their local node daemon via Unix socket gRPC:

```protobuf
service DevTeamDaemon {
  // Task operations
  rpc ClaimTask(ClaimTaskRequest) returns (ClaimTaskResponse);
  rpc UpdateTaskStatus(UpdateStatusRequest) returns (TaskResponse);
  rpc GetTask(GetTaskRequest) returns (TaskResponse);
  rpc ListTasks(ListTasksRequest) returns (ListTasksResponse);

  // Git operations
  rpc GitClone(GitCloneRequest) returns (GitResponse);
  rpc GitBranch(GitBranchRequest) returns (GitResponse);
  rpc GitCommit(GitCommitRequest) returns (GitResponse);
  rpc GitPush(GitPushRequest) returns (GitResponse);

  // Health
  rpc Heartbeat(HeartbeatRequest) returns (HeartbeatResponse);
}
```

Key messages:

- `ClaimTaskRequest { task_id, agent_id }` — atomic claim, returns success/failure
- `UpdateStatusRequest { task_id, status, detail }` — status change + history entry
- `GitCloneRequest { task_id, repo_url }` — clones into task workspace
- `HeartbeatRequest { agent_id, agent_type, current_task_id }` — periodic health check

## LLM Integration

### litellm as Unified Interface

All Python agents use `litellm` for LLM calls, providing a single API across:

- Ollama (local models)
- OpenAI-compatible servers (llama.cpp, vLLM, LocalAI)
- Cloud APIs (Anthropic, OpenAI)

### Provider Configuration

Per-agent LLM config with optional fallback:

```yaml
agents:
  - type: dev
    llm:
      primary:
        provider: ollama
        model: deepseek-coder-v3
      fallback:
        provider: anthropic
        model: claude-sonnet-4-6
      timeout: 120s
```

The node daemon passes LLM config to agents at spawn time. Agents don't need API keys or endpoints baked in — it's all config-driven.

### Shared Local Models

A GPU server running ollama can serve models to agents on other nodes over the network. An agent on a CPU-only VPS can use `endpoint: http://gpu-server:11434` to access powerful local models remotely.

## Project Structure

```
devTeam/
├── cmd/
│   └── devteam-node/
│       └── main.go              # Node daemon entrypoint
├── internal/
│   ├── daemon/
│   │   ├── daemon.go            # Core daemon lifecycle
│   │   ├── config.go            # YAML config parsing
│   │   └── config_test.go
│   ├── nats/
│   │   ├── cluster.go           # Embedded NATS setup + clustering
│   │   └── subjects.go          # Subject constants
│   ├── tasks/
│   │   ├── store.go             # SQLite task store
│   │   ├── sync.go              # Cross-node task sync via NATS
│   │   └── models.go            # Task struct + status types
│   ├── agents/
│   │   ├── manager.go           # Spawn/monitor/restart Python agents
│   │   └── registry.go          # Track which agents are on which nodes
│   ├── git/
│   │   └── workspace.go         # Clone, branch, push operations
│   └── api/
│       ├── grpc.go              # Unix socket gRPC server
│       └── proto/
│           └── devteam.proto    # Agent ↔ daemon protocol
├── agents/
│   ├── base/
│   │   ├── agent.py             # Base agent class
│   │   ├── llm.py               # litellm wrapper
│   │   └── comms.py             # gRPC client to daemon
│   ├── dev/
│   │   └── agent.py             # Dev agent logic
│   ├── pr_manager/
│   │   └── agent.py             # PR Manager + code review logic
│   ├── qa/
│   │   └── agent.py             # QA agent logic
│   └── deploy/
│       └── agent.py             # Deploy agent logic
├── config/
│   └── example.yaml             # Example node config
├── go.mod
├── go.sum
├── pyproject.toml               # Python agent dependencies
└── Makefile                     # Build Go binary + Python venv
```

## Node Configuration

```yaml
cluster:
  node_id: "server-a"
  seeds: ["server-b:4222"]

agents:
  - type: dev
    count: 2
    llm:
      primary:
        provider: ollama
        model: deepseek-coder-v3
        endpoint: http://localhost:11434
      fallback:
        provider: anthropic
        model: claude-sonnet-4-6
      timeout: 120s
  - type: qa
    count: 1
    llm:
      primary:
        provider: ollama
        model: qwen2.5-coder
        endpoint: http://localhost:11434

git:
  default_remote: "git@github.com:yourorg/yourrepo.git"
  workspace_dir: "/var/devteam/workspaces"

docker:
  registry: "registry.yourdomain.com"

deploy:
  method: "docker-compose"
  targets:
    - name: "production"
      host: "ssh://prod-server"
      compose_file: "docker-compose.prod.yml"
    - name: "staging"
      host: "ssh://staging-server"
      compose_file: "docker-compose.staging.yml"
  default_target: "staging"
```

## Intentionally Excluded (YAGNI)

These can be added later but are not part of the initial build:

- **Web dashboard** — node daemon could expose a REST API for this later
- **Auth/permissions between nodes** — start with trusted network/VPN
- **Agent scaling/load balancing** — NATS queue groups handle basic distribution
- **JetStream-level DLQ/retry policies** — application-level retry (3 attempts on agent crash) is built in, but JetStream dead-letter queues and advanced retry policies are deferred until failure patterns emerge
- **Progressive trust/autonomy** — human gates are fixed for now

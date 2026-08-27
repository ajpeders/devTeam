# devTeam Implementation Plan

Based on spec: `docs/superpowers/specs/2026-04-19-devteam-agentic-workflow-design.md`

## Task Ordering

Tasks are grouped into phases. Tasks within a phase can be parallelized.

### Phase 1: Foundation (no dependencies)

**Task 1: Project scaffolding + proto definition**
Set up Go module, Python project, and define the gRPC proto file.

Files to create:
- `go.mod` — module `github.com/alexdevteam/devteam`
- `go.sum` — empty initially
- `pyproject.toml` — Python project with dependencies: litellm, grpcio, grpcio-tools, nats-py, gitpython
- `Makefile` — targets: `build` (Go binary), `proto` (generate gRPC stubs), `venv` (Python virtualenv), `all`
- `internal/api/proto/devteam.proto` — full proto3 definition per spec: DevTeamDaemon service with ClaimTask, UpdateTaskStatus, GetTask, ListTasks, GitClone, GitBranch, GitCommit, GitPush, Heartbeat RPCs. Include all request/response message types.
- `config/example.yaml` — example node config per spec

**Task 2: Task data model + SQLite store (Go)**
Implement the task data model and SQLite persistence layer.

Files to create:
- `internal/tasks/models.go` — Task struct, TaskType enum (dev/review/qa/deploy), TaskStatus enum (pending/assigned/in_progress/blocked/needs_changes/completed/failed), TaskInput struct, HistoryEntry struct. Valid state transitions per spec's state machine.
- `internal/tasks/store.go` — SQLite-backed task store. CRUD operations: CreateTask, GetTask, ListTasks, UpdateStatus, ClaimTask (atomic assign + status change), AddHistory. Uses `modernc.org/sqlite` (pure Go, no CGo).
- `internal/tasks/store_test.go` — Tests for all store operations, especially ClaimTask atomicity and state transition validation.

**Task 3: NATS embedded cluster setup (Go)**
Set up embedded NATS server with clustering support.

Files to create:
- `internal/nats/cluster.go` — StartEmbeddedNATS function that takes cluster config (node_id, seeds, port) and returns a running embedded NATS server. Configures JetStream. Sets up TASKS stream for event sourcing.
- `internal/nats/subjects.go` — Constants for all NATS subjects per spec: task.created, task.assigned.{type}, task.completed.{id}, task.failed.{id}, review.requested.{id}, review.changes.{id}, qa.requested.{id}, deploy.requested.{id}, agent.heartbeat.{node}, task.ownership.transfer.
- `internal/nats/cluster_test.go` — Test embedded NATS starts, JetStream is available, subjects are well-formed.

### Phase 2: Core daemon (depends on Phase 1)

**Task 4: Task sync via JetStream (Go)**
Implement cross-node task state synchronization.

Files to create:
- `internal/tasks/sync.go` — TaskSyncer that publishes task mutations to JetStream TASKS stream and subscribes to replay them into local SQLite. Handles ownership transfer events. On startup, replays from last known sequence number. Event types: TaskCreated, TaskStatusChanged, TaskClaimed, OwnershipTransferred.
- `internal/tasks/sync_test.go` — Tests for publish, subscribe, replay-on-restart, ownership transfer.

**Task 5: Git workspace manager (Go)**
Implement git workspace isolation per task.

Files to create:
- `internal/git/workspace.go` — WorkspaceManager with methods: CreateWorkspace(taskID, repoURL) creates `{workspace_dir}/{task-id}/repo` using `--reference` to `.shared-clone/` bare repo. CleanupWorkspace(taskID). EnsureSharedClone(repoURL). CreateBranch, Commit, Push operations that shell out to git.
- `internal/git/workspace_test.go` — Tests for workspace creation, isolation, cleanup, shared clone reference.

**Task 6: Agent manager (Go)**
Manage Python agent process lifecycle.

Files to create:
- `internal/agents/manager.go` — AgentManager that spawns Python agent processes per config. Monitors heartbeats (30s timeout per spec). Restarts crashed agents. Passes LLM config as environment variables or CLI args to agent processes. Tracks agent_id → process mapping.
- `internal/agents/registry.go` — AgentRegistry tracking which agents are on which nodes across the cluster via NATS. Publishes local agent inventory, subscribes to remote inventories.
- `internal/agents/manager_test.go` — Tests for spawn, heartbeat monitoring, crash detection.

**Task 7: Config parsing (Go)**
Parse the YAML node configuration.

Files to create:
- `internal/daemon/config.go` — Config struct matching spec's YAML schema: ClusterConfig (node_id, seeds), AgentConfig (type, count, LLMConfig with primary/fallback/timeout), GitConfig (default_remote, workspace_dir), DockerConfig (registry), DeployConfig (method, targets with name/host/compose_file, default_target). ParseConfig(path) function.
- `internal/daemon/config_test.go` — Test parsing example.yaml, validation of required fields, defaults.

### Phase 3: gRPC API + Daemon assembly (depends on Phase 2)

**Task 8: gRPC server (Go)**
Implement the Unix socket gRPC server that agents connect to.

Files to create:
- `internal/api/grpc.go` — Implements DevTeamDaemon gRPC service. Wires up to TaskStore, WorkspaceManager, and NATS. Listens on Unix socket (`/var/run/devteam/daemon.sock` or configurable). Each RPC method delegates to the appropriate internal package.
- `internal/api/grpc_test.go` — Tests for each RPC method via in-process gRPC client.

**Task 9: Daemon main + lifecycle (Go)**
Wire everything together into the node daemon.

Files to create:
- `internal/daemon/daemon.go` — Daemon struct that owns all components: NATS, TaskStore, TaskSyncer, WorkspaceManager, AgentManager, GRPCServer. Start() initializes in order: config → NATS → JetStream → TaskStore → TaskSyncer → WorkspaceManager → AgentManager → GRPCServer. Stop() tears down in reverse. Signal handling (SIGTERM/SIGINT).
- `cmd/devteam-node/main.go` — CLI entrypoint. Parses `--config` flag, creates Daemon, calls Start(), waits for signal, calls Stop().

### Phase 4: Python agents (depends on proto from Phase 1, independent of Go phases)

**Task 10: Python agent base class**
Common foundation for all agent types.

Files to create:
- `agents/base/__init__.py`
- `agents/base/agent.py` — BaseAgent class: connects to daemon via gRPC Unix socket, subscribes to NATS subjects via daemon, implements heartbeat loop (10s interval), provides claim_task/update_status/complete_task/fail_task helpers. Abstract method `handle_task(task)` for subclasses. Graceful shutdown.
- `agents/base/llm.py` — LLMClient wrapping litellm. Takes provider config (provider, model, endpoint, api_key). call(messages, **kwargs) method. Handles primary/fallback with timeout.
- `agents/base/comms.py` — gRPC client stub for DevTeamDaemon service. Generated from proto + thin wrapper with retry logic.

**Task 11: Dev Agent**
Implements the code generation agent.

Files to create:
- `agents/dev/__init__.py`
- `agents/dev/agent.py` — DevAgent(BaseAgent). Subscribes to `task.assigned.dev` queue group. handle_task: requests git clone via daemon, creates branch, uses LLM to analyze task and generate code changes, commits, pushes, publishes `review.requested.{task-id}`. Handles `needs_changes` status (reads review comments, makes fixes, re-pushes).

**Task 12: PR Manager Agent**
Implements code review + PR management.

Files to create:
- `agents/pr_manager/__init__.py`
- `agents/pr_manager/agent.py` — PRManagerAgent(BaseAgent). Subscribes to `review.requested.{task-id}`. handle_task: reads the diff from git, uses LLM to review code (logic, style, security), creates GitHub PR via `requests` to GitHub API, posts review comments. If issues found: publishes `review.changes.{task-id}` to bounce back to Dev. If approved: adds `needs-human-review` label, polls for human approval, then publishes `qa.requested.{task-id}`.

**Task 13: QA Agent**
Implements test running and test generation.

Files to create:
- `agents/qa/__init__.py`
- `agents/qa/agent.py` — QAAgent(BaseAgent). Subscribes to `qa.requested.{task-id}`. handle_task: clones repo at PR branch, runs existing test suite (detects test runner: pytest, go test, npm test, etc.), uses LLM to analyze changes and write new tests, runs new tests, commits test files to PR branch, reports results. If tests fail: publishes `review.changes.{task-id}` to send back to Dev with failure context.

**Task 14: Deploy Agent**
Implements Docker build + deploy pipeline.

Files to create:
- `agents/deploy/__init__.py`
- `agents/deploy/agent.py` — DeployAgent(BaseAgent). Subscribes to `deploy.requested.{task-id}`. handle_task: merges PR (via GitHub API), clones merged code, builds Docker image (`docker build`), pushes to registry (`docker push`), deploys via docker-compose over SSH to configured target. Reports success/failure. No auto-retry on failure.

### Phase 5: CLI + integration (depends on everything)

**Task 15: CLI commands**
Add CLI subcommands beyond just running the daemon.

Files to modify:
- `cmd/devteam-node/main.go` — Add subcommands: `devteam-node run` (existing daemon start), `devteam-node submit <task-description> [--repo <url>] [--source issue|spec|natural_language]` (creates task via NATS), `devteam-node approve deploy <task-id>` (publishes deploy.requested), `devteam-node status [task-id]` (shows task status from local store).

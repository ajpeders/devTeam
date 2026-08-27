# MyDevTeam — Project Context

## What this is
Autonomous agentic dev team platform — 5 AI agents (orchestrator, dev, review, QA, deploy) collaborate on repos using local LLMs via ollama. FastAPI daemon + Python agents + NATS JetStream for multi-node. Orchestrator decomposes high-level tasks and coordinates N dev agents via hub-and-spoke.

## Entry point
```bash
python -m daemon.main --config config/local-test.yaml
```
Requires ollama running on localhost:11434. Agents are spawned as subprocesses.

## Key directories
- `daemon/` — API server, task store, NATS syncer, agent manager, git workspace
- `agents/` — Python agents (base, orchestrator, dev, pr_manager, qa, deploy)
- `config/` — YAML configs (local-test.yaml for dev)
- `docs/openapi.yaml` — API spec

## Tests
```bash
.venv/bin/python -m pytest daemon/api/test_server.py -x -q   # 26 API tests
.venv/bin/python -m pytest agents/ -q                          # 76 tests total (API + agents)
.venv/bin/python tests/e2e_multi_node.py                      # 2-node NATS test
```
Agent tests exist in `agents/*/test_agent.py` but may need ollama running.

## Config
- `config/local-test.yaml` — single-node dev config
- NATS is optional. Set `cluster.seeds: ["nats://host:4222"]` to enable multi-node.
- `cluster.seeds[0]` is used as the NATS URL (was previously hardcoded to localhost:4222).
- Legacy top-level `agents:` key auto-wrapped into a "default" project.

## Important patterns
- **Agent type `review`** maps to `agents.pr_manager.agent` via `MODULE_MAP` in `daemon/agents/manager.py`
- **`preexec_fn` for resource limits** — `resource.setrlimit` must be called in the child after fork but before exec, so it's passed as `preexec_fn` to `subprocess.Popen`
- **NATS sync is passive** — `_handle_event` applies remote events to local store but does NOT forward them. Events from one node propagate to all other nodes via NATS, but each node applies them independently.
- **NATS exactly-once** — subscribe uses `ConsumerConfig(deliver_policy="all", ack_policy="explicit", ack_wait=30, max_deliver=5)`. `_consume_loop` uses `msg.nak()` on error for redelivery, and a deduplication cache (`_seen`) keyed by `task_id:event:node_id` with 30s TTL.
- **Agent retry backoff** — `BaseAgent` uses `RetryStrategy` (max_retries=2, base_delay=2s, max_delay=60s, jitter). `handle_task_with_retries` retries `json.JSONDecodeError`, `ConnectionError`, `OSError`; does NOT retry `NotImplementedError`, `ValueError`. Claim loop backs off from 2s doubling to 60s max.
- **Tasks auto-chain** — completion of dev→review→QA creates next stage automatically. QA completion waits for human approval via `/api/deploy/approve`.
- **Orchestrator hub-and-spoke** — orchestrator decomposes tasks, assigns to devs, monitors children, relays context between devs. Devs never talk directly to each other.
- **DevSlot hot-swap** — dev agents read `DevSlot` config before each LLM call. Model changes mid-task, directory changes between tasks. No restart needed.
- **Task editing** — `PATCH /api/task/edit` with revision counter. In-progress agents check revision before each LLM call and re-read on change.
- **Two-layer retry** — BaseAgent retries transient errors (network, timeout). Orchestrator retries logical failures (bad code, wrong approach) across task attempts, max 3 retries.
- **SQLite WAL mode** — concurrent reads safe, writes serialized

## Gotchas
- qwen2.5:7b-instruct is unreliable for structured JSON — QA agent has raw Python fallback
- Docker group membership required for deploy agent — may need `newgrp docker` or daemon restart after `gpasswd -a $USER docker`
- Agent log files at `{workspace_dir}/logs/agent-{type}-{index}.log`
- `/api/task/claim` sorts by `(-priority, created_at)` — highest priority first, then oldest

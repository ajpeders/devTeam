# Roadmap

## Post-v0.1 (2026-05-11 → 2026-05-18)

- [x] **Dockerfile + `.dockerignore`** — multi-stage Python 3.12-slim build via `uv sync`, exposes 4223. Used by the parent monorepo's `docker-compose.yml`.
- [x] **`DEVTEAM_API_ADDRESS`** — runtime bind-address override. Set to `0.0.0.0:4223` for container/reverse-proxy use; otherwise defers to `api.address` in config.
- [x] **`MYDEVTEAM_ALLOWED_ORIGINS`** — comma-separated CORS origins, unioned with dev defaults.
- [x] **Fail-closed admin gate** — missing/invalid `X-Api-Key` returns 401 (no anonymous fallback). Admin resolution: `MYDEVTEAM_API_KEY` env match → synthetic admin → `MYDEVTEAM_ADMIN_EMAILS` → DB `is_admin`. Auth tests cover all three paths.
- [x] **Runtime deps fix** — declared `fastapi`, `uvicorn`, `sqlalchemy`, `pydantic` in `pyproject.toml` (previously transitive-only; broke clean installs).
- [x] **Test count** — API test suite now at 57 (up from 46 at v0.1).

## Released — 2026-05-11 (MyProject v0.1)

- Doc truth-up: README and HOWTO grep-verified against current code; test counts (21/26 → 38), OpenAPI version (v2.2.0 → v2.3.0), and planned-vs-shipped status updated.
- Orchestrator agent added to documented inventory (implementation was already in `agents/orchestrator/agent.py`; previously undocumented).

## Current Focus

### API Authentication & Key Management
- [x] Multiple API keys per user — shipped 2026-05-11. Additive `api_keys` table (sha256-hashed), endpoints at `/api/key/{create,list,delete}`. UserRow primary key untouched (backward compatible).
- [ ] Key scopes/permissions (read-only, submit, admin)
- [ ] Key rotation without downtime

### Sibling Context in LLM Prompts
- [x] Wire `sibling_context` into dev agent's `_plan_changes` prompt

## Planned

- [ ] Orchestrator chat interface (Web UI + CLI) — conversational task submission
- [ ] API rate limiting
- [ ] Task dependencies (block task B until task A completes)
- [ ] Agent metrics/telemetry export

## Done

- [x] Pure Python rewrite (Go removed)
- [x] User + Project models with API key auth
- [x] Per-project agent spawning with `DEVTEAM_PROJECT_ID`
- [x] Task pipeline chaining (dev → review → QA → human gate → deploy)
- [x] NATS JetStream multi-node sync with exactly-once delivery
- [x] Agent resource limits (RLIMIT_AS, RLIMIT_CPU)
- [x] Task priority + queuing
- [x] Webhooks (task.created, status_changed, completed, failed, approved)
- [x] Agent logs API
- [x] OpenAPI spec v2.3.0
- [x] Agent retry backoff (exponential with jitter)
- [x] Multi-project config format
- [x] CLI + Web UI split out as separate projects
- [x] Rebrand to MyDevTeam
- [x] Orchestrator agent (decomposition, hub-and-spoke, child monitoring, two-layer retry)
- [x] Multi-Dev Architecture / DevPool (DevSlot model, hot-swap, CRUD API, dynamic scaling)
- [x] Task editing (PATCH endpoint, parent_id, revision counter, mid-flight re-read)
- [x] Agent-internal endpoint auth (X-Agent-Key shared secret per boot)
- [x] Security audit fixes (path traversal, atomic claim, unbounded context cap, orchestrator fail-fast)

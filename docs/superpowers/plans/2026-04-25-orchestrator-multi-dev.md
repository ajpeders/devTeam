# Orchestrator + Multi-Dev Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an orchestrator agent that decomposes high-level tasks and coordinates N dynamically-configurable dev agents via hub-and-spoke.

**Architecture:** Orchestrator is a new agent type (subprocess) that claims `orchestrator` tasks, decomposes them via LLM into dev sub-tasks, monitors children, and relays context. Dev agents read mutable `DevSlot` configs before each LLM call for hot-swap. Tasks gain `parent_id` and `revision` fields for parent-child linking and mid-flight edit notification.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy, SQLite, litellm, pydantic

---

## File Structure

### New files
- `agents/orchestrator/__init__.py` — empty
- `agents/orchestrator/agent.py` — OrchestratorAgent class
- `agents/orchestrator/test_agent.py` — orchestrator unit tests

### Modified files
- `daemon/tasks/models.py` — add `TaskType.ORCHESTRATOR`, `Task.parent_id`, `Task.revision`, `DevSlot` model
- `daemon/tasks/store.py` — add `DevSlotRow`, `TaskRow.parent_id`, `TaskRow.revision`, store methods for dev slots + task editing
- `daemon/api/server.py` — add `PATCH /api/task/edit`, dev pool CRUD endpoints, update `TaskResp`
- `daemon/api/test_server.py` — tests for new endpoints
- `daemon/agents/manager.py` — add `DevPool` class, `MODULE_MAP["orchestrator"]`
- `daemon/config.py` — no changes needed (orchestrator uses existing `AgentConfig` with `type: orchestrator`)
- `agents/base/agent.py` — add `_check_task_revision()` and `_read_dev_slot()` methods
- `agents/base/llm.py` — add `update_config()` method for hot-swap
- `agents/dev/agent.py` — integrate revision checking before LLM calls

---

## Chunk 1: Data Model + Store Layer

### Task 1: Add new fields to Task model

**Files:**
- Modify: `daemon/tasks/models.py:12-17` (TaskType enum)
- Modify: `daemon/tasks/models.py:76-87` (Task model)
- Modify: `daemon/tasks/models.py:101-106` (TaskFilter)

- [ ] **Step 1: Add ORCHESTRATOR to TaskType enum**

In `daemon/tasks/models.py`, add to `TaskType`:

```python
class TaskType(str, enum.Enum):
    ORCHESTRATOR = "orchestrator"
    DEV = "dev"
    REVIEW = "review"
    QA = "qa"
    DEPLOY = "deploy"
```

- [ ] **Step 2: Add parent_id and revision to Task model**

```python
class Task(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    project_id: uuid.UUID | None = None
    parent_id: uuid.UUID | None = None  # orchestrator task that spawned this
    type: TaskType = TaskType.DEV
    status: TaskStatus = TaskStatus.PENDING
    input: TaskInput = Field(default_factory=TaskInput)
    assigned_to: str = ""
    node_id: str = ""
    priority: int = 0
    revision: int = 0  # incremented on task edit, agents check before LLM calls
    history: list[HistoryEntry] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
```

- [ ] **Step 3: Add parent_id filter to TaskFilter**

```python
class TaskFilter(BaseModel):
    type: TaskType | None = None
    status: TaskStatus | None = None
    node_id: str | None = None
    assigned_to: str | None = None
    project_id: uuid.UUID | None = None
    parent_id: uuid.UUID | None = None
```

- [ ] **Step 4: Run existing tests to verify no regressions**

Run: `cd /home/alex/projects/devTeam && .venv/bin/python -m pytest daemon/api/test_server.py -x -q`
Expected: All 26 tests pass (new fields have defaults, so existing code is unaffected)

- [ ] **Step 5: Commit**

```bash
git add daemon/tasks/models.py
git commit -m "feat: add TaskType.ORCHESTRATOR, Task.parent_id, Task.revision"
```

### Task 2: Add DevSlot model

**Files:**
- Modify: `daemon/tasks/models.py` (add DevSlot class at bottom)

- [ ] **Step 1: Add DevSlot pydantic model**

Append to `daemon/tasks/models.py`:

```python
class DevSlot(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    model: str = ""
    provider: str = "ollama"
    endpoint: str = ""
    working_dir: str = ""
    repo_url: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
```

- [ ] **Step 2: Commit**

```bash
git add daemon/tasks/models.py
git commit -m "feat: add DevSlot model for dynamic dev agent configuration"
```

### Task 3: Add DevSlotRow and update TaskRow in store

**Files:**
- Modify: `daemon/tasks/store.py:46-67` (TaskRow — add parent_id, revision columns)
- Modify: `daemon/tasks/store.py:137-160` (_row_to_task — read new columns)
- Modify: `daemon/tasks/store.py:283-310` (create_task — write new columns)
- Modify: `daemon/tasks/store.py` (add DevSlotRow class + CRUD methods)

- [ ] **Step 1: Add parent_id and revision to TaskRow**

In `daemon/tasks/store.py`, add to `TaskRow`:

```python
class TaskRow(Base):
    __tablename__ = "tasks"

    id = Column(String, primary_key=True)
    project_id = Column(String, ForeignKey("projects.id"), nullable=True)
    parent_id = Column(String, nullable=True)  # NEW
    type = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending")
    input = Column(Text, nullable=False, default="{}")
    assigned_to = Column(String, nullable=False, default="")
    node_id = Column(String, nullable=False, default="")
    priority = Column(Integer, nullable=False, default=0)
    revision = Column(Integer, nullable=False, default=0)  # NEW
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("idx_tasks_status", "status"),
        Index("idx_tasks_type", "type"),
        Index("idx_tasks_node_id", "node_id"),
        Index("idx_tasks_assigned_to", "assigned_to"),
        Index("idx_tasks_project_id", "project_id"),
        Index("idx_tasks_parent_id", "parent_id"),  # NEW
    )
```

- [ ] **Step 2: Update _row_to_task to read new columns**

```python
def _row_to_task(row: TaskRow, history: list[HistoryRow] | None = None) -> Task:
    task_input = TaskInput.model_validate_json(row.input) if row.input else TaskInput()
    entries = []
    if history:
        for h in history:
            entries.append(HistoryEntry(
                timestamp=h.timestamp,
                status=TaskStatus(h.status),
                detail=h.detail,
                agent_id=h.agent_id,
            ))
    return Task(
        id=uuid.UUID(row.id),
        project_id=uuid.UUID(row.project_id) if row.project_id else None,
        parent_id=uuid.UUID(row.parent_id) if row.parent_id else None,
        type=TaskType(row.type),
        status=TaskStatus(row.status),
        input=task_input,
        assigned_to=row.assigned_to,
        node_id=row.node_id,
        priority=row.priority or 0,
        revision=row.revision or 0,
        history=entries,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
```

- [ ] **Step 3: Update create_task to write new columns**

In `TaskStore.create_task`, add to the `TaskRow(...)` constructor:

```python
parent_id=str(task.parent_id) if task.parent_id else None,
revision=0,
```

- [ ] **Step 4: Update list_tasks to filter by parent_id**

In `TaskStore.list_tasks`, add after the existing filters:

```python
if task_filter.parent_id is not None:
    q = q.filter(TaskRow.parent_id == str(task_filter.parent_id))
```

- [ ] **Step 5: Add DevSlotRow**

Add after `WebhookRow`:

```python
class DevSlotRow(Base):
    __tablename__ = "dev_slots"

    id = Column(String, primary_key=True)
    model = Column(String, nullable=False, default="")
    provider = Column(String, nullable=False, default="ollama")
    endpoint = Column(String, nullable=False, default="")
    working_dir = Column(String, nullable=False, default="")
    repo_url = Column(String, nullable=False, default="")
    created_at = Column(DateTime, nullable=False)
```

Add converter:

```python
def _row_to_dev_slot(row: DevSlotRow) -> DevSlot:
    return DevSlot(
        id=row.id,
        model=row.model,
        provider=row.provider,
        endpoint=row.endpoint,
        working_dir=row.working_dir,
        repo_url=row.repo_url,
        created_at=row.created_at,
    )
```

- [ ] **Step 6: Add DevSlot CRUD methods to TaskStore**

Add to `TaskStore`:

```python
# ─── Dev Slots ────────────────────────────────────────────

def create_dev_slot(self, slot: DevSlot) -> DevSlot:
    now = datetime.now(timezone.utc)
    slot.created_at = now
    with self._session() as session:
        session.add(DevSlotRow(
            id=slot.id,
            model=slot.model,
            provider=slot.provider,
            endpoint=slot.endpoint,
            working_dir=slot.working_dir,
            repo_url=slot.repo_url,
            created_at=now,
        ))
        session.commit()
    return slot

def get_dev_slot(self, slot_id: str) -> DevSlot | None:
    with self._session() as session:
        row = session.query(DevSlotRow).filter_by(id=slot_id).first()
        return _row_to_dev_slot(row) if row else None

def list_dev_slots(self) -> list[DevSlot]:
    with self._session() as session:
        rows = session.query(DevSlotRow).order_by(DevSlotRow.created_at.asc()).all()
        return [_row_to_dev_slot(r) for r in rows]

def update_dev_slot(self, slot_id: str, **kwargs) -> DevSlot | None:
    with self._session() as session:
        row = session.query(DevSlotRow).filter_by(id=slot_id).first()
        if not row:
            return None
        for key, value in kwargs.items():
            if hasattr(row, key) and value is not None:
                setattr(row, key, value)
        session.commit()
        return _row_to_dev_slot(row)

def delete_dev_slot(self, slot_id: str) -> bool:
    with self._session() as session:
        row = session.query(DevSlotRow).filter_by(id=slot_id).first()
        if not row:
            return False
        session.delete(row)
        session.commit()
        return True
```

- [ ] **Step 7: Add edit_task method to TaskStore**

```python
def edit_task(self, task_id: uuid.UUID, description: str | None = None,
              params: dict | None = None, priority: int | None = None) -> Task:
    """Edit a task's description, params, or priority. Bumps revision."""
    with self._session() as session:
        row = session.query(TaskRow).filter_by(id=str(task_id)).first()
        if not row:
            raise ValueError(f"task not found: {task_id}")

        status = TaskStatus(row.status)
        if status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            raise ValueError(f"cannot edit task in {status.value} state")

        now = datetime.now(timezone.utc)
        task_input = TaskInput.model_validate_json(row.input) if row.input else TaskInput()

        if description is not None:
            task_input.description = description
        if params is not None:
            task_input.params.update(params)
        if priority is not None:
            row.priority = priority

        row.input = task_input.model_dump_json()
        row.revision = (row.revision or 0) + 1
        row.updated_at = now

        session.add(HistoryRow(
            task_id=str(task_id),
            timestamp=now,
            status=row.status,
            detail=f"task edited (revision {row.revision})",
        ))
        session.commit()

        history = (
            session.query(HistoryRow)
            .filter_by(task_id=str(task_id))
            .order_by(HistoryRow.id.asc())
            .all()
        )
        return _row_to_task(row, history)
```

- [ ] **Step 8: Add import for DevSlot in store.py**

Update the import at top of `store.py`:

```python
from .models import (
    DevSlot, HistoryEntry, Project, Task, TaskFilter, TaskInput, TaskStatus, TaskType, User, valid_transition,
    Webhook,
)
```

- [ ] **Step 9: Run tests**

Run: `cd /home/alex/projects/devTeam && .venv/bin/python -m pytest daemon/api/test_server.py -x -q`
Expected: All 26 tests pass. SQLite schema auto-migrates via `create_all`.

- [ ] **Step 10: Commit**

```bash
git add daemon/tasks/store.py
git commit -m "feat: add DevSlotRow, Task.parent_id/revision, edit_task and dev slot CRUD"
```

---

## Chunk 2: API Endpoints

### Task 4: Add task edit endpoint

**Files:**
- Modify: `daemon/api/server.py` — add `EditTaskReq`, `PATCH /api/task/edit`, update `TaskResp`
- Test: `daemon/api/test_server.py`

- [ ] **Step 1: Write failing tests for task editing**

Add to `daemon/api/test_server.py`:

```python
class TestTaskEditing:
    def _setup_project(self, client) -> tuple[str, dict]:
        reg = _create_user(client)
        h = _headers(reg["api_key"])
        resp = client.post("/api/project/create", headers=h, json={
            "name": "edit-test", "repo_url": "file:///tmp/repo",
        })
        return resp.json()["project_id"], h

    def test_edit_pending_task_description(self, client):
        project_id, h = self._setup_project(client)
        task_id = client.post("/api/task/create", headers=h, json={
            "project_id": project_id, "description": "Original",
        }).json()["task_id"]

        resp = client.patch("/api/task/edit", headers=h, json={
            "task_id": task_id, "description": "Updated",
        })
        assert resp.status_code == 200
        task = resp.json()["task"]
        assert task["input"]["description"] == "Updated"
        assert task["revision"] == 1

    def test_edit_task_params(self, client):
        project_id, h = self._setup_project(client)
        task_id = client.post("/api/task/create", headers=h, json={
            "project_id": project_id, "description": "Test",
        }).json()["task_id"]

        resp = client.patch("/api/task/edit", headers=h, json={
            "task_id": task_id, "params": {"branch": "feature-x"},
        })
        assert resp.status_code == 200
        assert resp.json()["task"]["input"]["params"]["branch"] == "feature-x"

    def test_edit_completed_task_fails(self, client):
        project_id, h = self._setup_project(client)
        task_id = client.post("/api/task/create", headers=h, json={
            "project_id": project_id, "description": "Done",
        }).json()["task_id"]

        # Move to completed via agent flow
        client.post("/api/task/claim", json={
            "agent_id": "dev-0", "agent_type": "dev", "project_id": project_id,
        })
        client.post("/api/task/status", json={
            "task_id": task_id, "status": "in_progress",
        })
        client.post("/api/task/status", json={
            "task_id": task_id, "status": "completed", "detail": "done",
        })

        resp = client.patch("/api/task/edit", headers=h, json={
            "task_id": task_id, "description": "Too late",
        })
        assert resp.status_code == 400

    def test_edit_increments_revision(self, client):
        project_id, h = self._setup_project(client)
        task_id = client.post("/api/task/create", headers=h, json={
            "project_id": project_id, "description": "Rev test",
        }).json()["task_id"]

        client.patch("/api/task/edit", headers=h, json={
            "task_id": task_id, "description": "Rev 1",
        })
        resp = client.patch("/api/task/edit", headers=h, json={
            "task_id": task_id, "description": "Rev 2",
        })
        assert resp.json()["task"]["revision"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/alex/projects/devTeam && .venv/bin/python -m pytest daemon/api/test_server.py::TestTaskEditing -x -q`
Expected: FAIL (endpoint doesn't exist yet)

- [ ] **Step 3: Add EditTaskReq model and update TaskResp**

In `daemon/api/server.py`, add request model:

```python
class EditTaskReq(BaseModel):
    task_id: str
    description: str | None = None
    params: dict[str, str] | None = None
    priority: int | None = None
```

Update `TaskResp` to include new fields:

```python
class TaskResp(BaseModel):
    id: str
    project_id: str = ""
    parent_id: str = ""  # NEW
    type: str
    status: str
    input: TaskInput
    assigned_to: str = ""
    node_id: str = ""
    priority: int = 0
    revision: int = 0  # NEW
    history: list[dict] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
```

Update `_task_resp` helper:

```python
def _task_resp(t: Task) -> TaskResp:
    return TaskResp(
        id=str(t.id),
        project_id=str(t.project_id) if t.project_id else "",
        parent_id=str(t.parent_id) if t.parent_id else "",
        type=t.type.value,
        status=t.status.value,
        input=t.input,
        assigned_to=t.assigned_to,
        node_id=t.node_id,
        priority=t.priority,
        revision=t.revision,
        history=[h.model_dump(mode="json") for h in t.history],
        created_at=t.created_at.isoformat() + "Z",
        updated_at=t.updated_at.isoformat() + "Z",
    )
```

- [ ] **Step 4: Add PATCH /api/task/edit endpoint**

Inside `_create_app`, after the priority endpoint:

```python
@app.patch("/api/task/edit")
async def edit_task(req: EditTaskReq, user: User = Depends(get_user)) -> dict:
    task_id = uuid.UUID(req.task_id)
    task = self.store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.project_id:
        self._verify_project_access(user, task.project_id)
    try:
        updated = self.store.edit_task(
            task_id,
            description=req.description,
            params=req.params,
            priority=req.priority,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"task": _task_resp(updated)}
```

- [ ] **Step 5: Run tests**

Run: `cd /home/alex/projects/devTeam && .venv/bin/python -m pytest daemon/api/test_server.py -x -q`
Expected: All tests pass (old 26 + new 4 = 30)

- [ ] **Step 6: Commit**

```bash
git add daemon/api/server.py daemon/api/test_server.py
git commit -m "feat: add PATCH /api/task/edit with revision counter"
```

### Task 5: Add dev pool API endpoints

**Files:**
- Modify: `daemon/api/server.py` — add dev slot CRUD endpoints
- Test: `daemon/api/test_server.py`

- [ ] **Step 1: Write failing tests for dev pool API**

Add to `daemon/api/test_server.py`:

```python
class TestDevPool:
    def test_create_dev_slot(self, client):
        resp = client.post("/api/dev/create", headers=_admin_headers(), json={
            "model": "qwen3:8b", "provider": "ollama",
            "endpoint": "http://localhost:11434", "repo_url": "file:///tmp/repo",
        })
        assert resp.status_code == 200
        assert resp.json()["id"]
        assert resp.json()["model"] == "qwen3:8b"

    def test_list_dev_slots(self, client):
        client.post("/api/dev/create", headers=_admin_headers(), json={
            "model": "qwen3:8b", "provider": "ollama",
        })
        resp = client.get("/api/dev/list", headers=_admin_headers())
        assert resp.status_code == 200
        assert len(resp.json()["slots"]) == 1

    def test_update_dev_slot(self, client):
        slot = client.post("/api/dev/create", headers=_admin_headers(), json={
            "model": "qwen3:8b", "provider": "ollama",
        }).json()

        resp = client.patch(f"/api/dev/{slot['id']}", headers=_admin_headers(), json={
            "model": "claude-sonnet-4-6", "provider": "anthropic",
        })
        assert resp.status_code == 200
        assert resp.json()["model"] == "claude-sonnet-4-6"

    def test_delete_dev_slot(self, client):
        slot = client.post("/api/dev/create", headers=_admin_headers(), json={
            "model": "qwen3:8b", "provider": "ollama",
        }).json()

        resp = client.delete(f"/api/dev/{slot['id']}", headers=_admin_headers())
        assert resp.status_code == 200

        slots = client.get("/api/dev/list", headers=_admin_headers()).json()["slots"]
        assert len(slots) == 0

    def test_non_admin_cannot_manage_dev_slots(self, client):
        user = _create_user(client)
        resp = client.post("/api/dev/create", headers=_headers(user["api_key"]), json={
            "model": "qwen3:8b", "provider": "ollama",
        })
        assert resp.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/alex/projects/devTeam && .venv/bin/python -m pytest daemon/api/test_server.py::TestDevPool -x -q`
Expected: FAIL

- [ ] **Step 3: Add request/response models**

In `daemon/api/server.py`:

```python
class CreateDevSlotReq(BaseModel):
    model: str = ""
    provider: str = "ollama"
    endpoint: str = ""
    working_dir: str = ""
    repo_url: str = ""

class UpdateDevSlotReq(BaseModel):
    model: str | None = None
    provider: str | None = None
    endpoint: str | None = None
    working_dir: str | None = None
    repo_url: str | None = None

class DevSlotResp(BaseModel):
    id: str
    model: str
    provider: str
    endpoint: str
    working_dir: str
    repo_url: str
    created_at: str
```

Add helper:

```python
def _dev_slot_resp(s) -> DevSlotResp:
    return DevSlotResp(
        id=s.id, model=s.model, provider=s.provider,
        endpoint=s.endpoint, working_dir=s.working_dir,
        repo_url=s.repo_url, created_at=s.created_at.isoformat() + "Z",
    )
```

- [ ] **Step 4: Add dev pool endpoints**

Inside `_create_app`, add after agent config endpoint:

```python
@app.post("/api/dev/create")
async def create_dev_slot(req: CreateDevSlotReq, user: User = Depends(get_user)) -> DevSlotResp:
    self._require_admin(user)
    from daemon.tasks.models import DevSlot
    slot = DevSlot(
        model=req.model, provider=req.provider, endpoint=req.endpoint,
        working_dir=req.working_dir, repo_url=req.repo_url,
    )
    self.store.create_dev_slot(slot)
    return _dev_slot_resp(slot)

@app.get("/api/dev/list")
async def list_dev_slots(user: User = Depends(get_user)) -> dict:
    self._require_admin(user)
    slots = self.store.list_dev_slots()
    return {"slots": [_dev_slot_resp(s) for s in slots]}

@app.patch("/api/dev/{slot_id}")
async def update_dev_slot(slot_id: str, req: UpdateDevSlotReq, user: User = Depends(get_user)) -> DevSlotResp:
    self._require_admin(user)
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    slot = self.store.update_dev_slot(slot_id, **updates)
    if not slot:
        raise HTTPException(status_code=404, detail="dev slot not found")
    return _dev_slot_resp(slot)

@app.delete("/api/dev/{slot_id}")
async def delete_dev_slot(slot_id: str, user: User = Depends(get_user)) -> dict:
    self._require_admin(user)
    deleted = self.store.delete_dev_slot(slot_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="dev slot not found")
    return {"ok": True}
```

- [ ] **Step 5: Add GET to CORS allow_methods**

Update the middleware config:

```python
allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
```

- [ ] **Step 6: Run tests**

Run: `cd /home/alex/projects/devTeam && .venv/bin/python -m pytest daemon/api/test_server.py -x -q`
Expected: All tests pass (30 + 5 = 35)

- [ ] **Step 7: Commit**

```bash
git add daemon/api/server.py daemon/api/test_server.py
git commit -m "feat: add dev pool CRUD API endpoints (create/list/update/delete)"
```

---

## Chunk 3: Agent Hot-Swap

### Task 6: Add LLM hot-swap to BaseAgent

**Files:**
- Modify: `agents/base/llm.py` — add `update_config()` method
- Modify: `agents/base/agent.py` — add `_check_task_revision()` and `_refresh_llm_config()` methods

- [ ] **Step 1: Add update_config to LLMClient**

In `agents/base/llm.py`:

```python
def update_config(self, config: dict):
    """Hot-swap LLM configuration. Takes effect on next chat() call."""
    self.primary = config["primary"]
    self.fallback = config.get("fallback")
    if "timeout" in config:
        self.timeout = config["timeout"]
```

- [ ] **Step 2: Add dev slot refresh to BaseAgent**

In `agents/base/agent.py`, add method:

```python
def _refresh_llm_config(self):
    """Read current DevSlot config from daemon and update LLM client if changed."""
    try:
        result = self._api_call("/api/dev/slot", {"agent_id": self.agent_id})
        if not result or not result.get("model"):
            return
        new_primary = {
            "provider": result["provider"],
            "model": result["model"],
            "endpoint": result.get("endpoint", ""),
            "api_key": result.get("api_key", ""),
        }
        current = self.llm_config.get("primary", {})
        if (new_primary["model"] != current.get("model") or
                new_primary["provider"] != current.get("provider") or
                new_primary["endpoint"] != current.get("endpoint")):
            self.log.info("hot-swap: %s/%s → %s/%s",
                          current.get("provider"), current.get("model"),
                          new_primary["provider"], new_primary["model"])
            self.llm_config["primary"] = new_primary
            if self.llm:
                self.llm.update_config(self.llm_config)
    except Exception:
        pass  # Non-fatal — keep using current config
```

- [ ] **Step 3: Add task revision checking to BaseAgent**

In `agents/base/agent.py`, add method:

```python
def _check_task_revision(self, task: dict) -> dict:
    """Check if task has been edited since last seen. Returns updated task if changed."""
    try:
        result = self._api_call("/api/task/get_internal", {"task_id": task["id"]})
        if not result or not result.get("task"):
            return task
        remote_task = result["task"]
        current_rev = task.get("revision", 0)
        remote_rev = remote_task.get("revision", 0)
        if remote_rev > current_rev:
            self.log.info("task %s edited (rev %d → %d), updating context",
                          task["id"][:8], current_rev, remote_rev)
            return remote_task
        return task
    except Exception:
        return task  # Non-fatal
```

- [ ] **Step 4: Commit**

```bash
git add agents/base/llm.py agents/base/agent.py
git commit -m "feat: add LLM hot-swap and task revision checking to BaseAgent"
```

### Task 7: Integrate hot-swap into DevAgent

**Files:**
- Modify: `agents/dev/agent.py` — check revision + refresh LLM before each call

- [ ] **Step 1: Update DevAgent._plan_changes to check revision and refresh LLM**

In `agents/dev/agent.py`, update `handle_task`:

```python
def handle_task(self, task: dict) -> None:
    task_id = task["id"]
    repo_url = self.input_value(task, "repo_url")
    content = self.input_value(task, "content")

    self.log.info("cloning repo %s", repo_url)
    workspace = self.git_clone(task_id, repo_url)

    branch_name = f"devteam/{task_id[:8]}"
    self.git_branch(workspace, branch_name)
    self.log.info("created branch %s", branch_name)

    # Check for task edits and LLM config changes before planning
    self._refresh_llm_config()
    task = self._check_task_revision(task)
    content = self.input_value(task, "content")

    self.log.info("planning changes via LLM")
    plan = self._plan_changes(content, workspace)
    file_count = len(plan.get("files", []))
    self.log.info("plan: %d file(s) to create/modify", file_count)

    # Check again before applying (task may have been edited during planning)
    self._refresh_llm_config()
    task = self._check_task_revision(task)
    content = self.input_value(task, "content")

    self._apply_changes(plan, workspace)

    self.git_commit(workspace, f"feat: {content[:72]}")
    self.git_push(workspace, branch_name)
    self.log.info("pushed branch %s", branch_name)

    self.update_status(task_id, "completed", message=f"branch:{branch_name}")
```

- [ ] **Step 2: Add internal task get endpoint for agents**

In `daemon/api/server.py`, add to agent-internal endpoints section:

```python
app.post("/api/task/get_internal")(self._get_task_internal)
```

Add handler:

```python
async def _get_task_internal(self, req: GetTaskReq) -> dict:
    task = self.store.get_task(uuid.UUID(req.task_id))
    if not task:
        return {"task": None}
    return {"task": _task_resp(task).model_dump()}
```

- [ ] **Step 3: Add dev slot lookup endpoint for agents**

In `daemon/api/server.py`, add to agent-internal endpoints:

```python
app.post("/api/dev/slot")(self._get_dev_slot)
```

Add handler:

```python
async def _get_dev_slot(self, req: dict) -> dict:
    agent_id = req.get("agent_id", "")
    # Look up slot by agent_id mapping (stored in agent manager)
    for mgr in self.agent_managers:
        slot = mgr.get_dev_slot_for_agent(agent_id)
        if slot:
            return {
                "model": slot.model, "provider": slot.provider,
                "endpoint": slot.endpoint, "repo_url": slot.repo_url,
            }
    return {}
```

- [ ] **Step 4: Run tests**

Run: `cd /home/alex/projects/devTeam && .venv/bin/python -m pytest daemon/api/test_server.py -x -q`
Expected: All tests pass

- [ ] **Step 5: Commit**

```bash
git add agents/dev/agent.py daemon/api/server.py
git commit -m "feat: integrate LLM hot-swap and revision checking into DevAgent"
```

---

## Chunk 4: Orchestrator Agent

### Task 8: Create orchestrator agent

**Files:**
- Create: `agents/orchestrator/__init__.py`
- Create: `agents/orchestrator/agent.py`

- [ ] **Step 1: Create empty init**

Create `agents/orchestrator/__init__.py` (empty file).

- [ ] **Step 2: Create OrchestratorAgent**

Create `agents/orchestrator/agent.py`:

```python
"""Orchestrator agent — decomposes high-level tasks and coordinates dev agents."""

from __future__ import annotations

import json
import time

from agents.base.agent import BaseAgent


class OrchestratorAgent(BaseAgent):
    """Claims orchestrator tasks, decomposes into dev sub-tasks, monitors children."""

    MONITOR_INTERVAL = 10  # seconds between child status checks
    MAX_RETRIES_PER_CHILD = 3

    def handle_task(self, task: dict) -> None:
        task_id = task["id"]
        description = self.input_value(task, "content")
        project_id = task.get("project_id", "")

        # 1. Get current dev pool state
        dev_pool = self._get_dev_pool()
        self.log.info("decomposing task with %d available devs", len(dev_pool))

        # 2. Decompose via LLM
        sub_tasks = self._decompose(description, dev_pool)
        self.log.info("decomposed into %d sub-tasks", len(sub_tasks))

        # 3. Create dev tasks
        child_ids = []
        for st in sub_tasks:
            child_id = self._create_child_task(project_id, task_id, st)
            child_ids.append(child_id)
            self.log.info("created sub-task %s: %s", child_id[:8], st.get("description", "")[:60])

        # 4. Monitor children until all complete or escalate
        self._monitor_children(task_id, project_id, child_ids)

    def _get_dev_pool(self) -> list[dict]:
        """Fetch current dev slot states from daemon."""
        try:
            result = self._api_call("/api/dev/pool_state", {})
            return result.get("slots", [])
        except Exception:
            return []

    def _decompose(self, description: str, dev_pool: list[dict]) -> list[dict]:
        """Use LLM to break a high-level task into dev sub-tasks."""
        pool_summary = json.dumps(dev_pool, indent=2) if dev_pool else "No dev agents configured yet."

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a tech lead. Break the task into independent sub-tasks for dev agents. "
                    "Each sub-task should be a self-contained unit of work.\n\n"
                    "Respond with JSON: {\"tasks\": [{\"description\": \"...\", \"params\": {}}]}\n\n"
                    "Keep sub-tasks small and focused. One feature or file per sub-task."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Task: {description}\n\n"
                    f"Available dev agents:\n{pool_summary}"
                ),
            },
        ]

        response = self.llm.chat(messages)
        try:
            parsed = json.loads(response)
            return parsed.get("tasks", [])
        except json.JSONDecodeError:
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                parsed = json.loads(response[start:end])
                return parsed.get("tasks", [])
            return [{"description": description, "params": {}}]

    def _create_child_task(self, project_id: str, parent_id: str, sub_task: dict) -> str:
        """Create a dev task as child of the orchestrator task."""
        result = self._api_call("/api/task/create_internal", {
            "project_id": project_id,
            "parent_id": parent_id,
            "type": "dev",
            "description": sub_task.get("description", ""),
            "params": sub_task.get("params", {}),
        })
        return result.get("task_id", "")

    def _monitor_children(self, task_id: str, project_id: str, child_ids: list[str]):
        """Poll child task statuses until all complete, fail, or need intervention."""
        retry_counts: dict[str, int] = {cid: 0 for cid in child_ids}

        while not self._stop_event.is_set():
            time.sleep(self.MONITOR_INTERVAL)

            children = self._get_children(task_id)
            if not children:
                continue

            all_done = True
            any_failed = False

            for child in children:
                child_id = child["id"]
                status = child["status"]

                if status in ("completed",):
                    # Relay output context to siblings still in progress
                    self._relay_context(child, children)
                elif status == "failed":
                    retries = retry_counts.get(child_id, 0)
                    if retries < self.MAX_RETRIES_PER_CHILD:
                        self.log.info("child %s failed (attempt %d), retrying", child_id[:8], retries + 1)
                        self._retry_child(project_id, task_id, child)
                        retry_counts[child_id] = retries + 1
                        all_done = False
                    else:
                        self.log.error("child %s exceeded max retries", child_id[:8])
                        any_failed = True
                elif status in ("pending", "assigned", "in_progress"):
                    all_done = False

            if all_done and not any_failed:
                self.log.info("all children completed successfully")
                self.update_status(task_id, "completed", message="all sub-tasks completed")
                return
            elif all_done and any_failed:
                self.log.error("some children failed after max retries")
                self.update_status(task_id, "failed", message="sub-task(s) failed after retries")
                return

    def _get_children(self, parent_id: str) -> list[dict]:
        """Fetch all child tasks for this orchestrator task."""
        try:
            result = self._api_call("/api/task/list_internal", {"parent_id": parent_id})
            return result.get("tasks", [])
        except Exception:
            return []

    def _relay_context(self, completed_child: dict, all_children: list[dict]):
        """Relay completed child's output to in-progress siblings."""
        detail = ""
        for h in completed_child.get("history", []):
            if h.get("status") == "completed":
                detail = h.get("detail", "")
                break

        if not detail:
            return

        for sibling in all_children:
            if sibling["id"] == completed_child["id"]:
                continue
            if sibling["status"] in ("pending", "assigned", "in_progress"):
                context = f"Context from sibling task: {detail}"
                existing_params = sibling.get("input", {}).get("params", {})
                existing_context = existing_params.get("sibling_context", "")
                new_context = f"{existing_context}\n{context}".strip()
                try:
                    self._api_call("/api/task/edit_internal", {
                        "task_id": sibling["id"],
                        "params": {"sibling_context": new_context},
                    })
                except Exception:
                    pass

    def _retry_child(self, project_id: str, parent_id: str, failed_child: dict):
        """Create a new dev task to retry the failed child's work."""
        description = failed_child.get("input", {}).get("description", "")
        detail = ""
        for h in failed_child.get("history", []):
            if h.get("status") == "failed":
                detail = h.get("detail", "")
                break

        params = dict(failed_child.get("input", {}).get("params", {}))
        params["retry_context"] = f"Previous attempt failed: {detail}"
        params["retry_count"] = str(int(params.get("retry_count", "0")) + 1)

        self._create_child_task(project_id, parent_id, {
            "description": description,
            "params": params,
        })


if __name__ == "__main__":
    agent = OrchestratorAgent()
    agent.run()
```

- [ ] **Step 3: Add orchestrator to MODULE_MAP**

In `daemon/agents/manager.py`, update:

```python
MODULE_MAP = {"review": "pr_manager", "orchestrator": "orchestrator"}
```

- [ ] **Step 4: Add internal endpoints for orchestrator**

In `daemon/api/server.py`, add to agent-internal endpoints:

```python
app.post("/api/task/create_internal")(self._create_task_internal)
app.post("/api/task/list_internal")(self._list_tasks_internal)
app.post("/api/task/edit_internal")(self._edit_task_internal)
app.post("/api/dev/pool_state")(self._get_pool_state)
```

Add handlers:

```python
async def _create_task_internal(self, req: dict) -> dict:
    """Internal endpoint for orchestrator to create child tasks."""
    task_type = TaskType(req.get("type", "dev"))
    parent_id = uuid.UUID(req["parent_id"]) if req.get("parent_id") else None
    project_id = uuid.UUID(req["project_id"]) if req.get("project_id") else None
    task = Task(
        project_id=project_id,
        parent_id=parent_id,
        type=task_type,
        node_id=self.node_id,
        input=TaskInput(
            description=req.get("description", ""),
            params=req.get("params", {}),
        ),
    )
    self.store.create_task(task)
    await self.syncer.publish_task_created(task)
    return {"task_id": str(task.id)}

async def _list_tasks_internal(self, req: dict) -> dict:
    """Internal endpoint to list tasks by parent_id."""
    parent_id = uuid.UUID(req["parent_id"]) if req.get("parent_id") else None
    tf = TaskFilter(parent_id=parent_id)
    tasks = self.store.list_tasks(tf)
    return {"tasks": [_task_resp(t).model_dump() for t in tasks]}

async def _edit_task_internal(self, req: dict) -> dict:
    """Internal endpoint for orchestrator to edit tasks."""
    task_id = uuid.UUID(req["task_id"])
    self.store.edit_task(
        task_id,
        description=req.get("description"),
        params=req.get("params"),
    )
    return {"ok": True}

async def _get_pool_state(self, req: dict) -> dict:
    """Return current dev slot state for orchestrator."""
    slots = self.store.list_dev_slots()
    return {"slots": [
        {"id": s.id, "model": s.model, "provider": s.provider,
         "endpoint": s.endpoint, "working_dir": s.working_dir, "repo_url": s.repo_url}
        for s in slots
    ]}
```

- [ ] **Step 5: Commit**

```bash
git add agents/orchestrator/ daemon/agents/manager.py daemon/api/server.py
git commit -m "feat: add orchestrator agent with task decomposition and child monitoring"
```

### Task 9: Write orchestrator tests

**Files:**
- Create: `agents/orchestrator/test_agent.py`

- [ ] **Step 1: Write orchestrator integration tests**

Create `agents/orchestrator/test_agent.py`:

```python
"""Tests for orchestrator agent task decomposition and monitoring."""

from __future__ import annotations

import uuid

import pytest
from starlette.testclient import TestClient

from daemon.api.server import DaemonServer
from daemon.nats.sync import TaskSyncer
from daemon.tasks.models import User
from daemon.tasks.store import TaskStore

ADMIN_KEY = "test-admin-key-12345"


@pytest.fixture()
def store(tmp_path):
    s = TaskStore(str(tmp_path / "test.db"))
    admin = User(name="admin", api_key=ADMIN_KEY, is_admin=True)
    s.create_user(admin)
    yield s
    s.close()


@pytest.fixture()
def client(store):
    syncer = TaskSyncer(None, store, "test-node")
    srv = DaemonServer(store=store, syncer=syncer, workspace=None, node_id="test-node")
    return TestClient(srv.app, raise_server_exceptions=False)


def _h() -> dict:
    return {"X-Api-Key": ADMIN_KEY}


class TestOrchestratorTaskFlow:
    def test_orchestrator_task_type_claimable(self, client):
        """Orchestrator tasks can be created and claimed."""
        project_id = client.post("/api/project/create", headers=_h(), json={
            "name": "orch-test", "repo_url": "file:///tmp/repo",
        }).json()["project_id"]

        # Create orchestrator task (via internal endpoint since user API defaults to dev)
        resp = client.post("/api/task/create_internal", json={
            "project_id": project_id, "type": "orchestrator",
            "description": "Build auth system",
        })
        assert resp.status_code == 200

        # Orchestrator agent claims it
        resp = client.post("/api/task/claim", json={
            "agent_id": "orchestrator-0", "agent_type": "orchestrator",
            "project_id": project_id,
        })
        assert resp.json()["found"] is True
        assert resp.json()["task"]["type"] == "orchestrator"

    def test_child_tasks_linked_by_parent_id(self, client):
        """Child tasks created with parent_id are retrievable."""
        project_id = client.post("/api/project/create", headers=_h(), json={
            "name": "parent-test", "repo_url": "file:///tmp/repo",
        }).json()["project_id"]

        parent_id = client.post("/api/task/create_internal", json={
            "project_id": project_id, "type": "orchestrator",
            "description": "Parent task",
        }).json()["task_id"]

        # Create child task
        child_id = client.post("/api/task/create_internal", json={
            "project_id": project_id, "parent_id": parent_id,
            "type": "dev", "description": "Child task",
        }).json()["task_id"]

        # List by parent
        children = client.post("/api/task/list_internal", json={
            "parent_id": parent_id,
        }).json()["tasks"]
        assert len(children) == 1
        assert children[0]["id"] == child_id
        assert children[0]["parent_id"] == parent_id

    def test_dev_pool_state_endpoint(self, client):
        """Pool state endpoint returns current dev slots."""
        from daemon.tasks.models import DevSlot
        client.post("/api/dev/create", headers=_h(), json={
            "model": "qwen3:8b", "provider": "ollama",
        })

        resp = client.post("/api/dev/pool_state", json={})
        assert resp.status_code == 200
        assert len(resp.json()["slots"]) == 1
        assert resp.json()["slots"][0]["model"] == "qwen3:8b"

    def test_edit_internal_bumps_revision(self, client):
        """Internal edit endpoint bumps task revision."""
        project_id = client.post("/api/project/create", headers=_h(), json={
            "name": "edit-int", "repo_url": "file:///tmp/repo",
        }).json()["project_id"]

        task_id = client.post("/api/task/create_internal", json={
            "project_id": project_id, "type": "dev",
            "description": "Original",
        }).json()["task_id"]

        client.post("/api/task/edit_internal", json={
            "task_id": task_id, "params": {"sibling_context": "some info"},
        })

        task = client.post("/api/task/get_internal", json={
            "task_id": task_id,
        }).json()["task"]
        assert task["revision"] == 1
```

- [ ] **Step 2: Run all tests**

Run: `cd /home/alex/projects/devTeam && .venv/bin/python -m pytest daemon/api/test_server.py agents/orchestrator/test_agent.py -x -q`
Expected: All tests pass

- [ ] **Step 3: Commit**

```bash
git add agents/orchestrator/test_agent.py
git commit -m "test: add orchestrator integration tests"
```

---

## Chunk 5: DevPool in AgentManager

### Task 10: Add DevPool to AgentManager

**Files:**
- Modify: `daemon/agents/manager.py` — add DevPool class, `get_dev_slot_for_agent` method

- [ ] **Step 1: Add DevPool class**

In `daemon/agents/manager.py`, add after `AgentProcess`:

```python
class DevPool:
    """Manages dynamic dev agent slots with mutable configs."""

    def __init__(self, store, agent_manager: AgentManager):
        self._store = store
        self._manager = agent_manager
        self._slot_to_agent: dict[str, str] = {}  # slot_id → agent_id

    def assign_slot(self, slot_id: str, agent_id: str):
        self._slot_to_agent[slot_id] = agent_id

    def get_slot_for_agent(self, agent_id: str):
        """Find the DevSlot assigned to this agent."""
        for slot_id, aid in self._slot_to_agent.items():
            if aid == agent_id:
                return self._store.get_dev_slot(slot_id)
        return None
```

- [ ] **Step 2: Add get_dev_slot_for_agent to AgentManager**

```python
def get_dev_slot_for_agent(self, agent_id: str):
    """Return the DevSlot for an agent, if managed by a DevPool."""
    if hasattr(self, 'dev_pool') and self.dev_pool:
        return self.dev_pool.get_slot_for_agent(agent_id)
    return None
```

Initialize `self.dev_pool = None` in `__init__`.

- [ ] **Step 3: Commit**

```bash
git add daemon/agents/manager.py
git commit -m "feat: add DevPool class for dynamic dev slot management"
```

---

## Chunk 6: Orchestrator Task Creation via User API

### Task 11: Allow users to submit orchestrator tasks

**Files:**
- Modify: `daemon/api/server.py` — update `CreateTaskReq` to accept optional `type` field

- [ ] **Step 1: Write failing test**

Add to `daemon/api/test_server.py`:

```python
class TestOrchestratorUserFlow:
    def test_create_orchestrator_task_via_api(self, client):
        reg = _create_user(client)
        h = _headers(reg["api_key"])

        project_id = client.post("/api/project/create", headers=h, json={
            "name": "orch-user", "repo_url": "file:///tmp/repo",
        }).json()["project_id"]

        resp = client.post("/api/task/create", headers=h, json={
            "project_id": project_id,
            "description": "Build complete auth system",
            "type": "orchestrator",
        })
        assert resp.status_code == 200

        task = client.post("/api/task/get", headers=h, json={
            "task_id": resp.json()["task_id"],
        }).json()["task"]
        assert task["type"] == "orchestrator"

    def test_default_type_is_dev(self, client):
        """Without type field, tasks default to dev (backward compat)."""
        reg = _create_user(client)
        h = _headers(reg["api_key"])

        project_id = client.post("/api/project/create", headers=h, json={
            "name": "default-type", "repo_url": "file:///tmp/repo",
        }).json()["project_id"]

        resp = client.post("/api/task/create", headers=h, json={
            "project_id": project_id, "description": "Simple task",
        })
        task = client.post("/api/task/get", headers=h, json={
            "task_id": resp.json()["task_id"],
        }).json()["task"]
        assert task["type"] == "dev"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/alex/projects/devTeam && .venv/bin/python -m pytest daemon/api/test_server.py::TestOrchestratorUserFlow -x -q`
Expected: FAIL

- [ ] **Step 3: Update CreateTaskReq and create_task endpoint**

Add `type` field to `CreateTaskReq`:

```python
class CreateTaskReq(BaseModel):
    project_id: str
    description: str
    params: dict[str, str] = Field(default_factory=dict)
    priority: int = 0
    type: str = "dev"
```

Update the `create_task` endpoint to use `req.type`:

```python
task = Task(
    project_id=project_id, type=TaskType(req.type), node_id=self.node_id,
    input=TaskInput(description=req.description, params=req.params),
    priority=req.priority,
)
```

- [ ] **Step 4: Run all tests**

Run: `cd /home/alex/projects/devTeam && .venv/bin/python -m pytest daemon/api/test_server.py agents/orchestrator/test_agent.py -x -q`
Expected: All tests pass

- [ ] **Step 5: Commit**

```bash
git add daemon/api/server.py daemon/api/test_server.py
git commit -m "feat: allow users to submit orchestrator tasks via API"
```

---

## Chunk 7: Final Integration + Docs

### Task 12: Update OpenAPI spec

**Files:**
- Modify: `docs/openapi.yaml`

- [ ] **Step 1: Add new endpoints to OpenAPI spec**

Add entries for:
- `PATCH /api/task/edit`
- `POST /api/dev/create`
- `GET /api/dev/list`
- `PATCH /api/dev/{id}`
- `DELETE /api/dev/{id}`
- Update task schema with `parent_id`, `revision`, `type: orchestrator`
- Bump version to `2.3.0`

- [ ] **Step 2: Commit**

```bash
git add docs/openapi.yaml
git commit -m "docs: update OpenAPI spec to v2.3.0 with orchestrator and dev pool endpoints"
```

### Task 13: Run full test suite

**Files:** none (verification only)

- [ ] **Step 1: Run all tests**

Run: `cd /home/alex/projects/devTeam && .venv/bin/python -m pytest daemon/api/test_server.py agents/orchestrator/test_agent.py -x -q`
Expected: All tests pass (35+ API tests + orchestrator tests)

- [ ] **Step 2: Verify backward compatibility**

Run: `cd /home/alex/projects/devTeam && .venv/bin/python -c "from daemon.config import parse_config; c = parse_config('config/local-test.yaml'); print('OK:', len(c.resolved_projects()), 'projects')"` 
Expected: `OK: 1 projects`

### Task 14: Final commit and summary

- [ ] **Step 1: Commit any remaining changes**

```bash
git add -A
git commit -m "feat: orchestrator + multi-dev architecture complete"
```

"""FastAPI server — all HTTP + WebSocket endpoints for the devteam daemon."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from daemon.api.webhooks import WebhookDispatcher
from daemon.agents.manager import AgentManager
from daemon.git.workspace import WorkspaceManager
from daemon.nats.sync import TaskSyncer
from daemon.tasks.models import Project, Task, TaskFilter, TaskInput, TaskStatus, TaskType, User
from daemon.tasks.store import TaskStore

logger = logging.getLogger(__name__)


# ─── Request / Response models ────────────────────────────────

class ClaimTaskReq(BaseModel):
    agent_id: str
    agent_type: str
    project_id: str = ""
    node_id: str = ""

class TaskResp(BaseModel):
    id: str
    project_id: str = ""
    parent_id: str = ""
    type: str
    status: str
    input: TaskInput
    assigned_to: str = ""
    node_id: str = ""
    priority: int = 0
    revision: int = 0
    history: list[dict] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

class ClaimTaskResp(BaseModel):
    found: bool
    task: TaskResp | None = None

class UpdateStatusReq(BaseModel):
    task_id: str
    status: str
    detail: str = ""
    message: str = ""

class GetTaskReq(BaseModel):
    task_id: str

class ListTasksReq(BaseModel):
    project_id: str = ""
    type: str = ""
    status: str = ""
    node_id: str = ""
    assigned_to: str = ""

class CreateTaskReq(BaseModel):
    project_id: str
    description: str
    params: dict[str, str] = Field(default_factory=dict)
    priority: int = 0
    type: str = "dev"  # "dev" or "orchestrator"

class CreateTaskResp(BaseModel):
    task_id: str

class CancelTaskReq(BaseModel):
    task_id: str

class RetryTaskReq(BaseModel):
    task_id: str

class SetPriorityReq(BaseModel):
    task_id: str
    priority: int

class DeleteTaskReq(BaseModel):
    task_id: str

class ApproveDeployReq(BaseModel):
    task_id: str

class ApproveDeployResp(BaseModel):
    deploy_task_id: str
    message: str = ""

class EditTaskReq(BaseModel):
    task_id: str
    description: str | None = None
    params: dict[str, str] | None = None
    priority: int | None = None

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

class HeartbeatReq(BaseModel):
    agent_id: str

class AgentLogsReq(BaseModel):
    agent_id: str
    tail_lines: int = 100

class DashboardStatsResp(BaseModel):
    total: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)
    by_status: dict[str, int] = Field(default_factory=dict)
    pending: int = 0
    in_progress: int = 0
    completed: int = 0
    failed: int = 0

class NodeInfoResp(BaseModel):
    node_id: str
    uptime_seconds: int
    agent_count: int
    nats_connected: bool
    api_address: str

# User / Project request models
class UserResp(BaseModel):
    id: str
    name: str
    email: str
    is_admin: bool = False
    created_at: str

class CreateUserReq(BaseModel):
    name: str
    email: str = ""

class CreateUserResp(BaseModel):
    user_id: str
    api_key: str

class DeleteUserReq(BaseModel):
    user_id: str

class CreateApiKeyReq(BaseModel):
    label: str = ""

class CreateApiKeyResp(BaseModel):
    id: str
    label: str
    key: str  # plaintext, returned once
    created_at: str

class ApiKeyResp(BaseModel):
    id: str
    label: str
    created_at: str

class DeleteApiKeyReq(BaseModel):
    key_id: str

class CreateProjectReq(BaseModel):
    name: str
    repo_url: str

class ProjectResp(BaseModel):
    id: str
    user_id: str
    name: str
    repo_url: str
    created_at: str

class CreateProjectResp(BaseModel):
    project_id: str

class DeleteProjectReq(BaseModel):
    project_id: str

# Webhook request/response models
class CreateWebhookReq(BaseModel):
    project_id: str
    url: str
    events: list[str] = Field(default_factory=list)
    active: bool = True


class WebhookResp(BaseModel):
    id: str
    project_id: str
    url: str
    events: list[str]
    secret: str
    active: bool
    created_at: str


class ListWebhooksResp(BaseModel):
    webhooks: list[WebhookResp]


class DeleteWebhookReq(BaseModel):
    webhook_id: str


# ─── Helpers ──────────────────────────────────────────────────

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


def _dev_slot_resp(s) -> DevSlotResp:
    return DevSlotResp(
        id=s.id, model=s.model, provider=s.provider,
        endpoint=s.endpoint, working_dir=s.working_dir,
        repo_url=s.repo_url, created_at=s.created_at.isoformat() + "Z",
    )


def _project_resp(p: Project) -> ProjectResp:
    return ProjectResp(
        id=str(p.id),
        user_id=str(p.user_id) if p.user_id else "",
        name=p.name,
        repo_url=p.repo_url,
        created_at=p.created_at.isoformat() + "Z",
    )


def _copy_params(task: Task) -> dict[str, str]:
    return dict(task.input.params)


def _detail_value(detail: str, prefix: str) -> str:
    for part in detail.split("|"):
        if part.startswith(prefix):
            return part[len(prefix):].strip()
    return ""


# ─── Server class ─────────────────────────────────────────────

class DaemonServer:
    """Wraps FastAPI app with all daemon state."""

    def __init__(
        self,
        store: TaskStore,
        syncer: TaskSyncer,
        workspace: WorkspaceManager | None,
        agent_mgr: AgentManager | None = None,
        node_id: str = "",
        api_address: str = "",
        agent_managers: list[AgentManager] | None = None,
        agent_secret: str = "",
    ):
        self.store = store
        self.syncer = syncer
        self.workspace = workspace
        # Support both single manager (backward compat) and list
        self.agent_managers: list[AgentManager] = agent_managers or ([agent_mgr] if agent_mgr else [])
        self.node_id = node_id
        self.api_address = api_address
        self.agent_secret = agent_secret
        self.start_time = time.time()
        self._ws_clients: set[WebSocket] = set()
        self._webhooks = WebhookDispatcher(store)

        self.app = self._create_app()

    def _get_user(self, x_api_key: str = Header()) -> User:
        """Dependency: authenticate user via X-Api-Key header.

        Resolution order:
          1. If MYDEVTEAM_API_KEY is set in the environment AND the presented key
             matches it, the caller is treated as a synthetic admin. This is the
             "platform admin" override and is the documented way to gate admin
             endpoints without provisioning a DB user first.
          2. Otherwise the key is looked up against the per-user store (primary
             UserRow.api_key column, then the additive api_keys table).

        Fail-closed: an empty/missing X-Api-Key is rejected (401). The env override
        is only consulted when MYDEVTEAM_API_KEY is non-empty — if it's unset, the
        override is unavailable and admin endpoints rely solely on DB-resident
        admin users.
        """
        if not x_api_key:
            raise HTTPException(status_code=401, detail="invalid API key")

        env_key = os.environ.get("MYDEVTEAM_API_KEY", "")
        if env_key and x_api_key == env_key:
            # Synthetic platform-admin user — not persisted, only used to satisfy
            # admin-gated endpoints. The id is a stable namespaced UUID5 so multiple
            # requests resolve to the same identity for any code that compares ids.
            return User(
                id=uuid.uuid5(uuid.NAMESPACE_DNS, "mydevteam.platform-admin"),
                name="platform-admin",
                email="",
                api_key=env_key,
                is_admin=True,
            )

        user = self.store.get_user_by_api_key(x_api_key)
        if not user:
            raise HTTPException(status_code=401, detail="invalid API key")

        # Auto-admin elevation by email allowlist (MYDEVTEAM_ADMIN_EMAILS).
        # Comma-separated, case-insensitive. Empty/unset = no auto-admins (safe default).
        # This only promotes the in-memory User returned to handlers — it does not
        # mutate the DB. Persistent admin status is set explicitly via admin endpoints
        # or seeded at daemon start.
        if not user.is_admin and user.email:
            raw = os.environ.get("MYDEVTEAM_ADMIN_EMAILS", "")
            if raw:
                allowlist = {e.strip().lower() for e in raw.split(",") if e.strip()}
                if user.email.lower() in allowlist:
                    user.is_admin = True
        return user

    def _verify_agent_key(self, x_agent_key: str = Header(default="")) -> str:
        """Dependency: authenticate agent via X-Agent-Key header.

        Fail-closed: if the daemon was started without an agent_secret (empty or unset),
        ALL agent-internal endpoints are rejected. Operating without a shared secret
        would let any caller reach internal endpoints like /api/task/claim and
        /api/task/status, which can mutate task state. Refusing is safer than allowing.
        """
        if not self.agent_secret or x_agent_key != self.agent_secret:
            raise HTTPException(status_code=403, detail="invalid agent key")
        return x_agent_key

    def _verify_project_access(self, user: User, project_id: uuid.UUID) -> Project:
        """Verify user owns the project (or is admin), return it."""
        project = self.store.get_project(project_id)
        if not project:
            raise HTTPException(status_code=404, detail="project not found")
        if not user.is_admin and project.user_id != user.id:
            raise HTTPException(status_code=404, detail="project not found")
        return project

    def _require_admin(self, user: User):
        """Raise 403 if user is not admin."""
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="admin access required")

    def _create_app(self) -> FastAPI:
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            if self.syncer.enabled:
                await self.syncer.start()
            broadcast_task = asyncio.create_task(self._ws_broadcast_loop())
            yield
            broadcast_task.cancel()
            if self.syncer.enabled:
                await self.syncer.stop()

        app = FastAPI(title="MyDevTeam", lifespan=lifespan)

        # MYDEVTEAM_ALLOWED_ORIGINS: comma-separated list of extra allowed origins, unioned with dev defaults.
        _extra = [o.strip() for o in os.environ.get("MYDEVTEAM_ALLOWED_ORIGINS", "").split(",") if o.strip()]
        _allowed_origins = sorted({"http://localhost:5173", "http://127.0.0.1:5173", *_extra})
        app.add_middleware(
            CORSMiddleware,
            allow_origins=_allowed_origins,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "X-Api-Key", "X-Agent-Key"],
            max_age=86400,
        )

        # ─── Liveness (unauthenticated) ───────────────────
        # Deliberately auth-free and side-effect-free: container orchestrators need
        # to probe it without credentials. Reports process liveness only — it does
        # not claim the agents or the LLM backend are healthy.

        @app.get("/healthz")
        async def healthz() -> dict:
            return {"status": "ok", "node_id": self.node_id}

        # Capture self._get_user for use in Depends()
        get_user = self._get_user
        verify_agent = self._verify_agent_key
        agent_dep = [Depends(verify_agent)]

        # ─── Agent-internal (agent-key auth) ──────────────

        app.post("/api/task/claim", dependencies=agent_dep)(self._claim_task)
        app.post("/api/task/status", dependencies=agent_dep)(self._update_task_status)
        app.post("/api/heartbeat", dependencies=agent_dep)(self._heartbeat)
        app.post("/api/git/clone", dependencies=agent_dep)(self._git_clone)
        app.post("/api/git/branch", dependencies=agent_dep)(self._git_branch)
        app.post("/api/git/commit", dependencies=agent_dep)(self._git_commit)
        app.post("/api/git/push", dependencies=agent_dep)(self._git_push)
        app.post("/api/task/get_internal", dependencies=agent_dep)(self._get_task_internal)
        app.post("/api/task/create_internal", dependencies=agent_dep)(self._create_task_internal)
        app.post("/api/task/list_internal", dependencies=agent_dep)(self._list_tasks_internal)
        app.post("/api/task/edit_internal", dependencies=agent_dep)(self._edit_task_internal)
        app.post("/api/dev/pool_state", dependencies=agent_dep)(self._get_pool_state)
        app.post("/api/dev/slot", dependencies=agent_dep)(self._get_dev_slot)

        # ─── User-authenticated endpoints ─────────────────
        # Inline closures so Depends(get_user) resolves correctly.

        @app.post("/api/user/me")
        async def get_me(user: User = Depends(get_user)) -> UserResp:
            return UserResp(
                id=str(user.id), name=user.name, email=user.email,
                is_admin=user.is_admin,
                created_at=user.created_at.isoformat() + "Z",
            )

        # ─── API key management (per-user additional keys) ────────

        @app.post("/api/key/create")
        async def create_api_key(req: CreateApiKeyReq, user: User = Depends(get_user)) -> CreateApiKeyResp:
            key_meta, plaintext = self.store.create_api_key(user.id, label=req.label)
            return CreateApiKeyResp(
                id=str(key_meta.id),
                label=key_meta.label,
                key=plaintext,
                created_at=key_meta.created_at.isoformat() + "Z",
            )

        @app.post("/api/key/list")
        async def list_api_keys(user: User = Depends(get_user)) -> dict:
            keys = self.store.list_api_keys(user.id)
            return {"keys": [ApiKeyResp(
                id=str(k.id), label=k.label,
                created_at=k.created_at.isoformat() + "Z",
            ) for k in keys]}

        @app.post("/api/key/delete")
        async def delete_api_key(req: DeleteApiKeyReq, user: User = Depends(get_user)) -> dict:
            try:
                key_id = uuid.UUID(req.key_id)
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid key_id")
            deleted = self.store.delete_api_key(key_id, user.id)
            if not deleted:
                raise HTTPException(status_code=404, detail="api key not found")
            return {"ok": True}

        # ─── Admin endpoints ─────────────────────────────

        @app.post("/api/admin/user/create")
        async def admin_create_user(req: CreateUserReq, user: User = Depends(get_user)) -> CreateUserResp:
            self._require_admin(user)
            new_user = User(name=req.name, email=req.email)
            self.store.create_user(new_user)
            return CreateUserResp(user_id=str(new_user.id), api_key=new_user.api_key)

        @app.post("/api/admin/user/list")
        async def admin_list_users(user: User = Depends(get_user)) -> dict:
            self._require_admin(user)
            users = self.store.list_users()
            return {"users": [UserResp(
                id=str(u.id), name=u.name, email=u.email,
                is_admin=u.is_admin,
                created_at=u.created_at.isoformat() + "Z",
            ) for u in users]}

        @app.post("/api/admin/user/delete")
        async def admin_delete_user(req: DeleteUserReq, user: User = Depends(get_user)) -> dict:
            self._require_admin(user)
            user_id = uuid.UUID(req.user_id)
            if user_id == user.id:
                raise HTTPException(status_code=400, detail="cannot delete yourself")
            deleted = self.store.delete_user(user_id)
            if not deleted:
                raise HTTPException(status_code=404, detail="user not found")
            return {"ok": True}

        @app.post("/api/webhook/create")
        async def create_webhook(req: CreateWebhookReq, user: User = Depends(get_user)) -> WebhookResp:
            project_id = uuid.UUID(req.project_id)
            self._verify_project_access(user, project_id)
            from daemon.tasks.models import Webhook
            wh = Webhook(
                user_id=user.id,
                project_id=project_id,
                url=req.url,
                events=req.events,
                active=req.active,
            )
            self.store.create_webhook(wh)
            return WebhookResp(
                id=str(wh.id),
                project_id=str(wh.project_id),
                url=wh.url,
                events=wh.events,
                secret=wh.secret,
                active=wh.active,
                created_at=wh.created_at.isoformat() + "Z",
            )

        @app.post("/api/webhook/list")
        async def list_webhooks(req: dict, user: User = Depends(get_user)) -> ListWebhooksResp:
            project_id = uuid.UUID(req["project_id"]) if req.get("project_id") else None
            if project_id:
                self._verify_project_access(user, project_id)
            webhooks = self.store.list_webhooks(user.id, project_id)
            return ListWebhooksResp(webhooks=[WebhookResp(
                id=str(w.id),
                project_id=str(w.project_id) if w.project_id else "",
                url=w.url,
                events=w.events,
                secret=w.secret,
                active=w.active,
                created_at=w.created_at.isoformat() + "Z",
            ) for w in webhooks])

        @app.post("/api/webhook/delete")
        async def delete_webhook(req: DeleteWebhookReq, user: User = Depends(get_user)) -> dict:
            wh_id = uuid.UUID(req.webhook_id)
            deleted = self.store.delete_webhook(wh_id, user.id)
            if not deleted:
                raise HTTPException(status_code=404, detail="webhook not found")
            return {"ok": True}

        @app.post("/api/project/create")
        async def create_project(req: CreateProjectReq, user: User = Depends(get_user)) -> CreateProjectResp:
            project = Project(user_id=user.id, name=req.name, repo_url=req.repo_url)
            self.store.create_project(project)
            return CreateProjectResp(project_id=str(project.id))

        @app.post("/api/project/list")
        async def list_projects(user: User = Depends(get_user)) -> dict:
            projects = self.store.list_projects(user_id=user.id)
            return {"projects": [_project_resp(p) for p in projects]}

        @app.post("/api/project/delete")
        async def delete_project(req: DeleteProjectReq, user: User = Depends(get_user)) -> dict:
            project_id = uuid.UUID(req.project_id)
            self._verify_project_access(user, project_id)
            deleted = self.store.delete_project(project_id)
            return {"ok": deleted}

        @app.post("/api/task/create")
        async def create_task(req: CreateTaskReq, user: User = Depends(get_user)) -> CreateTaskResp:
            project_id = uuid.UUID(req.project_id)
            self._verify_project_access(user, project_id)
            task = Task(
                project_id=project_id, type=TaskType(req.type), node_id=self.node_id,
                input=TaskInput(description=req.description, params=req.params),
                priority=req.priority,
            )
            self.store.create_task(task)
            await self.syncer.publish_task_created(task)
            if task.project_id:
                from daemon.api.webhooks import build_webhook_payload
                payload = build_webhook_payload("task.created", task, "")
                asyncio.create_task(self._webhooks.dispatch("task.created", payload, task.project_id))
            return CreateTaskResp(task_id=str(task.id))

        @app.post("/api/task/get")
        async def get_task(req: GetTaskReq, user: User = Depends(get_user)) -> dict:
            task = self.store.get_task(uuid.UUID(req.task_id))
            if not task:
                raise HTTPException(status_code=404, detail="task not found")
            if task.project_id:
                self._verify_project_access(user, task.project_id)
            return {"task": _task_resp(task)}

        @app.post("/api/task/list")
        async def list_tasks(req: ListTasksReq, user: User = Depends(get_user)) -> dict:
            if req.project_id:
                pid = uuid.UUID(req.project_id)
                self._verify_project_access(user, pid)
                tf = TaskFilter(project_id=pid)
            else:
                projects = self.store.list_projects(user_id=user.id)
                all_tasks = []
                for p in projects:
                    tf = TaskFilter(project_id=p.id)
                    if req.type:
                        tf.type = TaskType(req.type)
                    if req.status:
                        tf.status = TaskStatus(req.status)
                    all_tasks.extend(self.store.list_tasks(tf))
                return {"tasks": [_task_resp(t) for t in all_tasks]}

            if req.type:
                tf.type = TaskType(req.type)
            if req.status:
                tf.status = TaskStatus(req.status)
            if req.node_id:
                tf.node_id = req.node_id
            if req.assigned_to:
                tf.assigned_to = req.assigned_to
            tasks = self.store.list_tasks(tf)
            return {"tasks": [_task_resp(t) for t in tasks]}

        @app.post("/api/task/cancel")
        async def cancel_task(req: CancelTaskReq, user: User = Depends(get_user)) -> dict:
            task = self.store.get_task(uuid.UUID(req.task_id))
            if not task:
                raise HTTPException(status_code=404, detail="task not found")
            if task.project_id:
                self._verify_project_access(user, task.project_id)
            self.store.update_status(task.id, TaskStatus.CANCELLED, "cancelled by user")
            await self.syncer.publish_status_changed(task.id, TaskStatus.CANCELLED, "cancelled")
            return {"ok": True}

        @app.post("/api/task/retry")
        async def retry_task(req: RetryTaskReq, user: User = Depends(get_user)) -> dict:
            task = self.store.get_task(uuid.UUID(req.task_id))
            if not task:
                raise HTTPException(status_code=404, detail="task not found")
            if task.project_id:
                self._verify_project_access(user, task.project_id)
            if task.status != TaskStatus.FAILED:
                raise HTTPException(status_code=400, detail=f"task is {task.status.value}, only failed tasks can be retried")
            self.store.update_status(task.id, TaskStatus.PENDING, "retried by user")
            await self.syncer.publish_status_changed(task.id, TaskStatus.PENDING, "retried")
            return {"ok": True}

        @app.patch("/api/task/priority")
        async def set_priority(req: SetPriorityReq, user: User = Depends(get_user)) -> dict:
            task = self.store.get_task(uuid.UUID(req.task_id))
            if not task:
                raise HTTPException(status_code=404, detail="task not found")
            if task.project_id:
                self._verify_project_access(user, task.project_id)
            updated = self.store.update_priority(uuid.UUID(req.task_id), req.priority)
            return {"task": _task_resp(updated)}

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
                    task_id, description=req.description,
                    params=req.params, priority=req.priority,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            return {"task": _task_resp(updated)}

        @app.post("/api/task/delete")
        async def delete_task(req: DeleteTaskReq, user: User = Depends(get_user)) -> dict:
            task = self.store.get_task(uuid.UUID(req.task_id))
            if not task:
                raise HTTPException(status_code=404, detail="task not found")
            if task.project_id:
                self._verify_project_access(user, task.project_id)
            self.store.delete_task(task.id)
            return {"ok": True}

        @app.post("/api/deploy/approve")
        async def approve_deploy(req: ApproveDeployReq, user: User = Depends(get_user)) -> ApproveDeployResp:
            task = self.store.get_task(uuid.UUID(req.task_id))
            if not task:
                raise HTTPException(status_code=404, detail="task not found")
            if task.project_id:
                self._verify_project_access(user, task.project_id)
            if task.type != TaskType.QA:
                raise HTTPException(status_code=400, detail=f"task is type {task.type.value}, expected qa")
            if task.status != TaskStatus.COMPLETED:
                raise HTTPException(status_code=400, detail=f"task is {task.status.value}, must be completed")
            params = _copy_params(task)
            params["approved_by"] = "human"
            deploy = Task(
                project_id=task.project_id, type=TaskType.DEPLOY, node_id=self.node_id,
                input=TaskInput(description=task.input.description, params=params),
            )
            self.store.create_task(deploy)
            await self.syncer.publish_task_created(deploy)
            if deploy.project_id:
                from daemon.api.webhooks import build_webhook_payload
                payload = build_webhook_payload("task.approved", deploy, "human approved")
                asyncio.create_task(self._webhooks.dispatch("task.approved", payload, deploy.project_id))
            return ApproveDeployResp(deploy_task_id=str(deploy.id), message="deploy task created")

        @app.post("/api/agents/list")
        async def list_agents(user: User = Depends(get_user)) -> dict:
            all_statuses = []
            for mgr in self.agent_managers:
                all_statuses.extend(mgr.list_statuses())
            return {"agents": all_statuses}

        @app.post("/api/agent/logs")
        async def agent_logs(req: AgentLogsReq, user: User = Depends(get_user)) -> dict:
            if not self.agent_managers:
                raise HTTPException(status_code=503, detail="agent manager not configured")

            for mgr in self.agent_managers:
                result = mgr.get_agent_log(req.agent_id, req.tail_lines)
                if result is not None:
                    content, total_lines = result
                    return {
                        "agent_id": req.agent_id,
                        "log": content,
                        "total_lines": total_lines,
                        "preview_lines": min(req.tail_lines, total_lines),
                    }

            # Agent not found on this node — return 200 with empty content
            return {
                "agent_id": req.agent_id,
                "log": "",
                "total_lines": 0,
                "preview_lines": 0,
                "error": f"agent {req.agent_id} not found on this node (may be on a different cluster node)",
            }

        @app.post("/api/agent/config")
        async def agent_config(req: dict, user: User = Depends(get_user)) -> dict:
            """Return the running config for an agent type."""
            if not self.agent_managers:
                raise HTTPException(status_code=503, detail="agent manager not configured")
            agent_type = req.get("type", "")
            for mgr in self.agent_managers:
                cfg = mgr.get_config_for_type(agent_type)
                if cfg:
                    return {
                        "type": cfg.type,
                        "max_memory_mb": cfg.max_memory_mb,
                        "max_cpu_percent": cfg.max_cpu_percent,
                        "llm_model": cfg.llm.primary.model,
                    }
            raise HTTPException(status_code=404, detail=f"no agent config for type {agent_type}")

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

        @app.post("/api/dashboard/stats")
        async def dashboard_stats(user: User = Depends(get_user)) -> DashboardStatsResp:
            projects = self.store.list_projects(user_id=user.id)
            all_tasks = []
            for p in projects:
                all_tasks.extend(self.store.list_tasks(TaskFilter(project_id=p.id)))
            by_type: dict[str, int] = {}
            by_status: dict[str, int] = {}
            for t in all_tasks:
                by_type[t.type.value] = by_type.get(t.type.value, 0) + 1
                by_status[t.status.value] = by_status.get(t.status.value, 0) + 1
            return DashboardStatsResp(
                total=len(all_tasks), by_type=by_type, by_status=by_status,
                pending=by_status.get("pending", 0), in_progress=by_status.get("in_progress", 0),
                completed=by_status.get("completed", 0), failed=by_status.get("failed", 0),
            )

        @app.post("/api/node/info")
        async def node_info(user: User = Depends(get_user)) -> NodeInfoResp:
            agent_count = sum(len(mgr.list_statuses()) for mgr in self.agent_managers)
            return NodeInfoResp(
                node_id=self.node_id, uptime_seconds=int(time.time() - self.start_time),
                agent_count=agent_count, nats_connected=self.syncer.connected,
                api_address=self.api_address,
            )

        # WebSocket
        app.websocket("/ws/tasks")(self._ws_tasks)

        return app

    # ─── Agent-internal handlers (no user auth) ───────────

    async def _get_task_internal(self, req: GetTaskReq) -> dict:
        task = self.store.get_task(uuid.UUID(req.task_id))
        if not task:
            return {"task": None}
        return {"task": _task_resp(task).model_dump()}

    async def _create_task_internal(self, req: dict) -> dict:
        task_type = TaskType(req.get("type", "dev"))
        parent_id = uuid.UUID(req["parent_id"]) if req.get("parent_id") else None
        project_id = uuid.UUID(req["project_id"]) if req.get("project_id") else None
        task = Task(
            project_id=project_id, parent_id=parent_id, type=task_type,
            node_id=self.node_id,
            input=TaskInput(description=req.get("description", ""), params=req.get("params", {})),
        )
        self.store.create_task(task)
        await self.syncer.publish_task_created(task)
        return {"task_id": str(task.id)}

    async def _list_tasks_internal(self, req: dict) -> dict:
        parent_id = uuid.UUID(req["parent_id"]) if req.get("parent_id") else None
        tf = TaskFilter(parent_id=parent_id)
        tasks = self.store.list_tasks(tf)
        return {"tasks": [_task_resp(t).model_dump() for t in tasks]}

    async def _edit_task_internal(self, req: dict) -> dict:
        task_id = uuid.UUID(req["task_id"])
        self.store.edit_task(task_id, description=req.get("description"), params=req.get("params"))
        return {"ok": True}

    async def _get_pool_state(self, req: dict) -> dict:
        slots = self.store.list_dev_slots()
        return {"slots": [
            {"id": s.id, "model": s.model, "provider": s.provider,
             "endpoint": s.endpoint, "working_dir": s.working_dir, "repo_url": s.repo_url}
            for s in slots
        ]}

    async def _get_dev_slot(self, req: dict) -> dict:
        agent_id = req.get("agent_id", "")
        for mgr in self.agent_managers:
            slot = mgr.get_dev_slot_for_agent(agent_id)
            if slot:
                return {"model": slot.model, "provider": slot.provider,
                        "endpoint": slot.endpoint, "repo_url": slot.repo_url}
        return {}

    async def _claim_task(self, req: ClaimTaskReq) -> ClaimTaskResp:
        task_type = TaskType(req.agent_type)
        tf = TaskFilter(type=task_type, status=TaskStatus.PENDING)
        if req.project_id:
            tf.project_id = uuid.UUID(req.project_id)

        tasks = self.store.list_tasks(tf)
        if not tasks:
            return ClaimTaskResp(found=False)

        # Pick highest priority, then oldest; iterate on race
        tasks.sort(key=lambda t: (-t.priority, t.created_at))
        node_id = req.node_id or self.node_id
        for candidate in tasks:
            claimed = self.store.claim_task(candidate.id, req.agent_id, node_id)
            if claimed:
                await self.syncer.publish_task_claimed(claimed.id, req.agent_id, node_id)
                return ClaimTaskResp(found=True, task=_task_resp(claimed))
        return ClaimTaskResp(found=False)

    async def _update_task_status(self, req: UpdateStatusReq) -> dict:
        task_id = uuid.UUID(req.task_id)
        new_status = TaskStatus(req.status)
        detail = req.detail or req.message

        task = self.store.update_status(task_id, new_status, detail)
        await self.syncer.publish_status_changed(task_id, new_status, detail)

        # Dispatch webhooks for status changes
        if task.project_id:
            from daemon.api.webhooks import build_webhook_payload
            payload = build_webhook_payload(f"task.{new_status.value}", task, detail)
            await self._webhooks.dispatch(f"task.status_changed", payload, task.project_id)
            if new_status == TaskStatus.COMPLETED:
                await self._webhooks.dispatch("task.completed", payload, task.project_id)
            elif new_status == TaskStatus.FAILED:
                await self._webhooks.dispatch("task.failed", payload, task.project_id)

        if new_status == TaskStatus.COMPLETED:
            await self._handle_task_completion(task, detail)

        return {"ok": True}

    async def _heartbeat(self, req: HeartbeatReq) -> dict:
        for mgr in self.agent_managers:
            mgr.record_heartbeat(req.agent_id)
        return {"ok": True}

    async def _git_clone(self, req: dict) -> dict:
        if not self.workspace:
            return {"error": "workspace manager not configured"}
        path = self.workspace.create_workspace(req["task_id"], req.get("repo_url", ""))
        return {"workspace_path": path}

    async def _git_branch(self, req: dict) -> dict:
        if not self.workspace:
            return {"error": "workspace manager not configured"}
        self.workspace.create_branch(req["workspace_path"], req["branch_name"])
        return {"ok": True}

    async def _git_commit(self, req: dict) -> dict:
        if not self.workspace:
            return {"error": "workspace manager not configured"}
        self.workspace.commit_all(req["workspace_path"], req["message"])
        return {"ok": True}

    async def _git_push(self, req: dict) -> dict:
        if not self.workspace:
            return {"error": "workspace manager not configured"}
        self.workspace.push(req["workspace_path"], req["branch_name"])
        return {"ok": True}

    # ─── Pipeline chaining ─────────────────────────────────

    async def _handle_task_completion(self, task: Task, detail: str):
        if task.type == TaskType.DEV:
            params = _copy_params(task)
            branch = _detail_value(detail, "branch:")
            if branch:
                params["branch"] = branch
            await self._create_child_task(task, TaskType.REVIEW, params)

        elif task.type == TaskType.REVIEW:
            params = _copy_params(task)
            pr_url = _detail_value(detail, "pr:")
            if pr_url:
                params["pr_url"] = pr_url

            if "changes_requested" in detail:
                params["review_result"] = "changes_requested"
                params["review_detail"] = detail
                await self._create_child_task(task, TaskType.DEV, params)
            else:
                params["review_result"] = "approved"
                if "approved" not in detail:
                    logger.warning(
                        "review task %s completed without explicit approved/changes_requested, "
                        "defaulting to approved: %r", task.id, detail,
                    )
                await self._create_child_task(task, TaskType.QA, params)

    async def _create_child_task(self, parent: Task, task_type: TaskType, params: dict[str, str]):
        params["parent_id"] = str(parent.id)
        child = Task(
            project_id=parent.project_id,
            type=task_type,
            node_id=self.node_id,
            input=TaskInput(description=parent.input.description, params=params),
        )
        self.store.create_task(child)
        await self.syncer.publish_task_created(child)
        logger.info("chained %s task %s from parent %s", task_type.value, child.id, parent.id)

    # ─── WebSocket ─────────────────────────────────────────

    async def _ws_tasks(self, ws: WebSocket):
        await ws.accept()
        self._ws_clients.add(ws)
        try:
            await self._send_ws_update(ws)
            while True:
                data = await ws.receive_json()
                if data.get("type") == "subscribe":
                    await self._send_ws_update(ws)
        except WebSocketDisconnect:
            pass
        finally:
            self._ws_clients.discard(ws)

    async def _send_ws_update(self, ws: WebSocket):
        tasks = self.store.list_tasks()
        stats: dict[str, int] = {}
        for t in tasks:
            stats[t.status.value] = stats.get(t.status.value, 0) + 1

        await ws.send_json({
            "type": "update",
            "tasks": [_task_resp(t).model_dump() for t in tasks],
            "stats": stats,
        })

    async def _ws_broadcast_loop(self):
        while True:
            await asyncio.sleep(2)
            dead = []
            for ws in list(self._ws_clients):
                try:
                    await self._send_ws_update(ws)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._ws_clients.discard(ws)

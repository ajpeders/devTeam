"""SQLAlchemy-backed task store with SQLite."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .models import (
    ApiKey, DevSlot, HistoryEntry, Project, Task, TaskFilter, TaskInput, TaskStatus, TaskType, User, valid_transition,
    Webhook,
)


def _hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


class Base(DeclarativeBase):
    pass


class UserRow(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False, default="")
    api_key = Column(String, nullable=False, unique=True)
    is_admin = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False)


class ProjectRow(Base):
    __tablename__ = "projects"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=True)
    name = Column(String, nullable=False)
    repo_url = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("idx_projects_user_id", "user_id"),
    )


class TaskRow(Base):
    __tablename__ = "tasks"

    id = Column(String, primary_key=True)
    project_id = Column(String, ForeignKey("projects.id"), nullable=True)
    parent_id = Column(String, nullable=True)
    type = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending")
    input = Column(Text, nullable=False, default="{}")
    assigned_to = Column(String, nullable=False, default="")
    node_id = Column(String, nullable=False, default="")
    priority = Column(Integer, nullable=False, default=0)
    revision = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("idx_tasks_status", "status"),
        Index("idx_tasks_type", "type"),
        Index("idx_tasks_node_id", "node_id"),
        Index("idx_tasks_assigned_to", "assigned_to"),
        Index("idx_tasks_project_id", "project_id"),
        Index("idx_tasks_parent_id", "parent_id"),
    )


class HistoryRow(Base):
    __tablename__ = "task_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey("tasks.id"), nullable=False)
    timestamp = Column(DateTime, nullable=False)
    status = Column(String, nullable=False)
    detail = Column(String, nullable=False, default="")
    agent_id = Column(String, nullable=False, default="")

    __table_args__ = (
        Index("idx_task_history_task_id", "task_id"),
    )


class WebhookRow(Base):
    __tablename__ = "webhooks"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    project_id = Column(String, ForeignKey("projects.id"), nullable=True)
    url = Column(String, nullable=False)
    events = Column(String, nullable=False, default="[]")  # JSON list
    secret = Column(String, nullable=False, default="")
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("idx_webhooks_user_id", "user_id"),
        Index("idx_webhooks_project_id", "project_id"),
    )


class ApiKeyRow(Base):
    __tablename__ = "api_keys"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    key_hash = Column(String, nullable=False)
    label = Column(String, nullable=False, default="")
    created_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("idx_api_keys_user_id", "user_id"),
        Index("idx_api_keys_key_hash", "key_hash"),
    )


class DevSlotRow(Base):
    __tablename__ = "dev_slots"
    id = Column(String, primary_key=True)
    model = Column(String, nullable=False, default="")
    provider = Column(String, nullable=False, default="ollama")
    endpoint = Column(String, nullable=False, default="")
    working_dir = Column(String, nullable=False, default="")
    repo_url = Column(String, nullable=False, default="")
    created_at = Column(DateTime, nullable=False)


def _row_to_user(row: UserRow) -> User:
    return User(
        id=uuid.UUID(row.id),
        name=row.name,
        email=row.email,
        api_key=row.api_key,
        is_admin=bool(row.is_admin),
        created_at=row.created_at,
    )


def _row_to_api_key(row: ApiKeyRow) -> ApiKey:
    return ApiKey(
        id=uuid.UUID(row.id),
        user_id=uuid.UUID(row.user_id),
        label=row.label,
        created_at=row.created_at,
    )


def _row_to_dev_slot(row: DevSlotRow) -> DevSlot:
    return DevSlot(
        id=row.id, model=row.model, provider=row.provider,
        endpoint=row.endpoint, working_dir=row.working_dir,
        repo_url=row.repo_url, created_at=row.created_at,
    )


def _row_to_webhook(row: WebhookRow) -> Webhook:
    import json as _json
    return Webhook(
        id=uuid.UUID(row.id),
        user_id=uuid.UUID(row.user_id),
        project_id=uuid.UUID(row.project_id) if row.project_id else None,
        url=row.url,
        events=_json.loads(row.events) if row.events else [],
        secret=row.secret,
        active=bool(row.active),
        created_at=row.created_at,
    )


def _row_to_project(row: ProjectRow) -> Project:
    return Project(
        id=uuid.UUID(row.id),
        user_id=uuid.UUID(row.user_id) if row.user_id else None,
        name=row.name,
        repo_url=row.repo_url,
        created_at=row.created_at,
    )


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


class TaskStore:
    """SQLite-backed persistence for projects and tasks."""

    def __init__(self, db_path: str):
        self.engine = create_engine(f"sqlite:///{db_path}", echo=False)

        @event.listens_for(self.engine, "connect")
        def set_wal(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self._session_factory = sessionmaker(bind=self.engine)

    def _session(self) -> Session:
        return self._session_factory()

    def close(self):
        self.engine.dispose()

    # ─── Users ─────────────────────────────────────────────

    def create_user(self, user: User) -> User:
        now = datetime.now(timezone.utc)
        user.created_at = now
        with self._session() as session:
            session.add(UserRow(
                id=str(user.id),
                name=user.name,
                email=user.email,
                api_key=user.api_key,
                is_admin=user.is_admin,
                created_at=now,
            ))
            session.commit()
        return user

    def get_user(self, user_id: uuid.UUID) -> User | None:
        with self._session() as session:
            row = session.query(UserRow).filter_by(id=str(user_id)).first()
            return _row_to_user(row) if row else None

    def get_user_by_api_key(self, api_key: str) -> User | None:
        """Resolve a presented key to a User. Checks the primary `UserRow.api_key`
        column first (legacy plaintext), then the additive `api_keys` table
        (sha256-hashed). Either match returns the owning user."""
        with self._session() as session:
            row = session.query(UserRow).filter_by(api_key=api_key).first()
            if row:
                return _row_to_user(row)
            key_hash = _hash_api_key(api_key)
            ak_row = session.query(ApiKeyRow).filter_by(key_hash=key_hash).first()
            if not ak_row:
                return None
            user_row = session.query(UserRow).filter_by(id=ak_row.user_id).first()
            return _row_to_user(user_row) if user_row else None

    def list_users(self) -> list[User]:
        with self._session() as session:
            rows = session.query(UserRow).order_by(UserRow.created_at.asc()).all()
            return [_row_to_user(r) for r in rows]

    def delete_user(self, user_id: uuid.UUID) -> bool:
        with self._session() as session:
            row = session.query(UserRow).filter_by(id=str(user_id)).first()
            if not row:
                return False
            # Delete user's projects and their tasks
            projects = session.query(ProjectRow).filter_by(user_id=str(user_id)).all()
            for p in projects:
                tasks = session.query(TaskRow).filter_by(project_id=p.id).all()
                for t in tasks:
                    session.query(HistoryRow).filter_by(task_id=t.id).delete()
                    session.delete(t)
                session.delete(p)
            session.delete(row)
            session.commit()
            return True

    # ─── API Keys (additive, beyond the per-user primary UserRow.api_key) ──

    def create_api_key(self, user_id: uuid.UUID, label: str = "") -> tuple[ApiKey, str]:
        """Issue a new API key for a user. Returns (metadata, plaintext-shown-once).
        The plaintext is the only chance to capture the key — only its sha256 hash
        is stored. Subsequent reads return metadata only."""
        plaintext = uuid.uuid4().hex
        key_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        with self._session() as session:
            session.add(ApiKeyRow(
                id=str(key_id),
                user_id=str(user_id),
                key_hash=_hash_api_key(plaintext),
                label=label,
                created_at=now,
            ))
            session.commit()
        return ApiKey(id=key_id, user_id=user_id, label=label, created_at=now), plaintext

    def list_api_keys(self, user_id: uuid.UUID) -> list[ApiKey]:
        with self._session() as session:
            rows = (
                session.query(ApiKeyRow)
                .filter_by(user_id=str(user_id))
                .order_by(ApiKeyRow.created_at.asc())
                .all()
            )
            return [_row_to_api_key(r) for r in rows]

    def delete_api_key(self, key_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        with self._session() as session:
            row = session.query(ApiKeyRow).filter_by(id=str(key_id), user_id=str(user_id)).first()
            if not row:
                return False
            session.delete(row)
            session.commit()
            return True

    # ─── Projects ──────────────────────────────────────────

    def create_project(self, project: Project) -> Project:
        now = datetime.now(timezone.utc)
        project.created_at = now
        with self._session() as session:
            session.add(ProjectRow(
                id=str(project.id),
                user_id=str(project.user_id) if project.user_id else None,
                name=project.name,
                repo_url=project.repo_url,
                created_at=now,
            ))
            session.commit()
        return project

    def get_project(self, project_id: uuid.UUID) -> Project | None:
        with self._session() as session:
            row = session.query(ProjectRow).filter_by(id=str(project_id)).first()
            return _row_to_project(row) if row else None

    def get_project_by_name(self, name: str) -> Project | None:
        with self._session() as session:
            row = session.query(ProjectRow).filter_by(name=name).first()
            return _row_to_project(row) if row else None

    def list_projects(self, user_id: uuid.UUID | None = None) -> list[Project]:
        with self._session() as session:
            q = session.query(ProjectRow)
            if user_id is not None:
                q = q.filter(ProjectRow.user_id == str(user_id))
            rows = q.order_by(ProjectRow.created_at.asc()).all()
            return [_row_to_project(r) for r in rows]

    def delete_project(self, project_id: uuid.UUID) -> bool:
        with self._session() as session:
            row = session.query(ProjectRow).filter_by(id=str(project_id)).first()
            if not row:
                return False
            # Delete associated tasks and their history
            tasks = session.query(TaskRow).filter_by(project_id=str(project_id)).all()
            for t in tasks:
                session.query(HistoryRow).filter_by(task_id=t.id).delete()
                session.delete(t)
            session.delete(row)
            session.commit()
            return True

    # ─── Tasks ─────────────────────────────────────────────

    def create_task(self, task: Task) -> Task:
        now = datetime.now(timezone.utc)
        task.status = TaskStatus.PENDING
        task.created_at = now
        task.updated_at = now

        with self._session() as session:
            row = TaskRow(
                id=str(task.id),
                project_id=str(task.project_id) if task.project_id else None,
                parent_id=str(task.parent_id) if task.parent_id else None,
                type=task.type.value,
                status=task.status.value,
                input=task.input.model_dump_json(),
                assigned_to=task.assigned_to,
                node_id=task.node_id,
                priority=task.priority,
                revision=0,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.add(HistoryRow(
                task_id=str(task.id),
                timestamp=now,
                status=TaskStatus.PENDING.value,
                detail="task created",
            ))
            session.commit()
        return task

    def get_task(self, task_id: uuid.UUID) -> Task | None:
        with self._session() as session:
            row = session.query(TaskRow).filter_by(id=str(task_id)).first()
            if not row:
                return None
            history = (
                session.query(HistoryRow)
                .filter_by(task_id=str(task_id))
                .order_by(HistoryRow.id.asc())
                .all()
            )
            return _row_to_task(row, history)

    def list_tasks(self, task_filter: TaskFilter | None = None) -> list[Task]:
        with self._session() as session:
            q = session.query(TaskRow)
            if task_filter:
                if task_filter.type is not None:
                    q = q.filter(TaskRow.type == task_filter.type.value)
                if task_filter.status is not None:
                    q = q.filter(TaskRow.status == task_filter.status.value)
                if task_filter.node_id is not None:
                    q = q.filter(TaskRow.node_id == task_filter.node_id)
                if task_filter.assigned_to is not None:
                    q = q.filter(TaskRow.assigned_to == task_filter.assigned_to)
                if task_filter.project_id is not None:
                    q = q.filter(TaskRow.project_id == str(task_filter.project_id))
                if task_filter.parent_id is not None:
                    q = q.filter(TaskRow.parent_id == str(task_filter.parent_id))
            rows = q.order_by(TaskRow.created_at.asc()).all()
            return [_row_to_task(r) for r in rows]

    def update_status(self, task_id: uuid.UUID, new_status: TaskStatus, detail: str = "") -> Task:
        with self._session() as session:
            row = session.query(TaskRow).filter_by(id=str(task_id)).first()
            if not row:
                raise ValueError(f"task not found: {task_id}")

            current = TaskStatus(row.status)
            if not valid_transition(current, new_status):
                raise ValueError(f"invalid transition from {current.value} to {new_status.value}")

            now = datetime.now(timezone.utc)
            row.status = new_status.value
            row.updated_at = now
            if new_status == TaskStatus.PENDING:
                row.assigned_to = ""

            session.add(HistoryRow(
                task_id=str(task_id),
                timestamp=now,
                status=new_status.value,
                detail=detail,
            ))
            session.commit()

            history = (
                session.query(HistoryRow)
                .filter_by(task_id=str(task_id))
                .order_by(HistoryRow.id.asc())
                .all()
            )
            return _row_to_task(row, history)

    def claim_task(self, task_id: uuid.UUID, agent_id: str, node_id: str) -> Task | None:
        """Atomically claim a pending task. Returns None if already claimed."""
        with self._session() as session:
            updated = (
                session.query(TaskRow)
                .filter_by(id=str(task_id), status=TaskStatus.PENDING.value)
                .update({
                    TaskRow.status: TaskStatus.ASSIGNED.value,
                    TaskRow.assigned_to: agent_id,
                    TaskRow.node_id: node_id,
                    TaskRow.updated_at: datetime.now(timezone.utc),
                })
            )
            if not updated:
                return None

            now = datetime.now(timezone.utc)
            session.add(HistoryRow(
                task_id=str(task_id),
                timestamp=now,
                status=TaskStatus.ASSIGNED.value,
                detail=f"claimed by {agent_id} on {node_id}",
                agent_id=agent_id,
            ))
            session.commit()

            row = session.query(TaskRow).filter_by(id=str(task_id)).first()
            history = (
                session.query(HistoryRow)
                .filter_by(task_id=str(task_id))
                .order_by(HistoryRow.id.asc())
                .all()
            )
            return _row_to_task(row, history)

    def delete_task(self, task_id: uuid.UUID) -> bool:
        with self._session() as session:
            row = session.query(TaskRow).filter_by(id=str(task_id)).first()
            if not row:
                return False
            session.query(HistoryRow).filter_by(task_id=str(task_id)).delete()
            session.delete(row)
            session.commit()
            return True

    def update_node_id(self, task_id: uuid.UUID, new_node_id: str):
        with self._session() as session:
            row = session.query(TaskRow).filter_by(id=str(task_id)).first()
            if not row:
                raise ValueError(f"task not found: {task_id}")
            row.node_id = new_node_id
            row.updated_at = datetime.now(timezone.utc)
            session.commit()

    def update_priority(self, task_id: uuid.UUID, priority: int) -> Task:
        with self._session() as session:
            row = session.query(TaskRow).filter_by(id=str(task_id)).first()
            if not row:
                raise ValueError(f"task not found: {task_id}")
            row.priority = priority
            row.updated_at = datetime.now(timezone.utc)
            session.commit()

            history = (
                session.query(HistoryRow)
                .filter_by(task_id=str(task_id))
                .order_by(HistoryRow.id.asc())
                .all()
            )
            return _row_to_task(row, history)

    # ─── Webhooks ─────────────────────────────────────────────

    def create_webhook(self, webhook: Webhook) -> Webhook:
        now = datetime.now(timezone.utc)
        webhook.created_at = now
        import json as _json
        with self._session() as session:
            session.add(WebhookRow(
                id=str(webhook.id),
                user_id=str(webhook.user_id),
                project_id=str(webhook.project_id) if webhook.project_id else None,
                url=webhook.url,
                events=_json.dumps(webhook.events),
                secret=webhook.secret,
                active=webhook.active,
                created_at=now,
            ))
            session.commit()
        return webhook

    def list_webhooks(self, user_id: uuid.UUID, project_id: uuid.UUID | None = None) -> list[Webhook]:
        import json as _json
        with self._session() as session:
            q = session.query(WebhookRow).filter_by(user_id=str(user_id))
            if project_id:
                q = q.filter(WebhookRow.project_id == str(project_id))
            rows = q.order_by(WebhookRow.created_at.asc()).all()
            return [_row_to_webhook(r) for r in rows]

    def get_webhook(self, webhook_id: uuid.UUID, user_id: uuid.UUID) -> Webhook | None:
        with self._session() as session:
            row = session.query(WebhookRow).filter_by(id=str(webhook_id), user_id=str(user_id)).first()
            return _row_to_webhook(row) if row else None

    def delete_webhook(self, webhook_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        with self._session() as session:
            row = session.query(WebhookRow).filter_by(id=str(webhook_id), user_id=str(user_id)).first()
            if not row:
                return False
            session.delete(row)
            session.commit()
            return True

    def get_active_webhooks(self, project_id: uuid.UUID, event: str) -> list[Webhook]:
        import json as _json
        with self._session() as session:
            rows = session.query(WebhookRow).filter(
                WebhookRow.project_id == str(project_id),
                WebhookRow.active == True,
            ).all()
            result = []
            for row in rows:
                wh = _row_to_webhook(row)
                if event in wh.events or "*" in wh.events:
                    result.append(wh)
            return result

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
                task_id=str(task_id), timestamp=now, status=row.status,
                detail=f"task edited (revision {row.revision})",
            ))
            session.commit()
            history = (
                session.query(HistoryRow).filter_by(task_id=str(task_id))
                .order_by(HistoryRow.id.asc()).all()
            )
            return _row_to_task(row, history)

    # ─── Dev Slots ────────────────────────────────────────────

    def create_dev_slot(self, slot: DevSlot) -> DevSlot:
        now = datetime.now(timezone.utc)
        slot.created_at = now
        with self._session() as session:
            session.add(DevSlotRow(
                id=slot.id, model=slot.model, provider=slot.provider,
                endpoint=slot.endpoint, working_dir=slot.working_dir,
                repo_url=slot.repo_url, created_at=now,
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

"""Task models, status enum, and state machine for the MyDevTeam pipeline."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field


class TaskType(str, enum.Enum):
    ORCHESTRATOR = "orchestrator"
    DEV = "dev"
    REVIEW = "review"
    QA = "qa"
    DEPLOY = "deploy"


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    NEEDS_CHANGES = "needs_changes"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# State machine: maps each status to its valid next statuses.
VALID_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING: {TaskStatus.ASSIGNED, TaskStatus.CANCELLED},
    TaskStatus.ASSIGNED: {TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED},
    TaskStatus.IN_PROGRESS: {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED},
    TaskStatus.BLOCKED: {TaskStatus.PENDING, TaskStatus.CANCELLED},
    TaskStatus.NEEDS_CHANGES: {TaskStatus.PENDING, TaskStatus.CANCELLED},
    TaskStatus.COMPLETED: {TaskStatus.NEEDS_CHANGES},
    TaskStatus.FAILED: {TaskStatus.PENDING},  # retry
    TaskStatus.CANCELLED: set(),  # terminal
}


def valid_transition(from_status: TaskStatus, to_status: TaskStatus) -> bool:
    return to_status in VALID_TRANSITIONS.get(from_status, set())


class TaskInput(BaseModel):
    description: str = ""
    params: dict[str, str] = Field(default_factory=dict)


class HistoryEntry(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: TaskStatus = TaskStatus.PENDING
    detail: str = ""
    agent_id: str = ""


class User(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    name: str
    email: str = ""
    api_key: str = Field(default_factory=lambda: uuid.uuid4().hex)
    is_admin: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ApiKey(BaseModel):
    """Metadata for an additional API key beyond UserRow.api_key.

    Plaintext key is never stored on the model — only its sha256 hash lives in the
    api_keys table. The plaintext is returned exactly once at issue time."""
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    user_id: uuid.UUID
    label: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Project(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    user_id: uuid.UUID | None = None
    name: str
    repo_url: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Task(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    project_id: uuid.UUID | None = None
    parent_id: uuid.UUID | None = None  # orchestrator task that spawned this
    type: TaskType = TaskType.DEV
    status: TaskStatus = TaskStatus.PENDING
    input: TaskInput = Field(default_factory=TaskInput)
    assigned_to: str = ""
    node_id: str = ""
    priority: int = 0  # higher = more urgent
    revision: int = 0  # incremented on task edit, agents check before LLM calls
    history: list[HistoryEntry] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Webhook(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    user_id: uuid.UUID
    project_id: uuid.UUID | None = None
    url: str
    events: list[str] = Field(default_factory=list)  # ["task.created", "task.status_changed", ...]
    secret: str = Field(default_factory=lambda: uuid.uuid4().hex[:32])
    active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TaskFilter(BaseModel):
    type: TaskType | None = None
    status: TaskStatus | None = None
    node_id: str | None = None
    assigned_to: str | None = None
    project_id: uuid.UUID | None = None
    parent_id: uuid.UUID | None = None


class DevSlot(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    model: str = ""
    provider: str = "ollama"
    endpoint: str = ""
    working_dir: str = ""
    repo_url: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

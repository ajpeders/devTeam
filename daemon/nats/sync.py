"""Optional NATS JetStream task syncer for multi-node setups."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from daemon.tasks.models import Task, TaskStatus, TaskType, TaskInput

logger = logging.getLogger(__name__)

try:
    import nats
    from nats.js import JetStreamContext
    from nats.js.api import ConsumerConfig
    NATS_AVAILABLE = True
except ImportError:
    NATS_AVAILABLE = False


class TaskSyncer:
    """Publishes task events to NATS JetStream and subscribes to remote events.

    If NATS is not configured or nats-py is not installed, all methods are no-ops.
    """

    def __init__(self, nats_url: str | None, store: Any, node_id: str):
        self.nats_url = nats_url
        self.store = store
        self.node_id = node_id
        self._nc = None
        self._js: JetStreamContext | None = None
        self._sub = None
        self._running = False
        # Deduplication cache: "task_id:event:node_id" -> timestamp
        self._seen: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.nats_url and NATS_AVAILABLE)

    @property
    def connected(self) -> bool:
        return self._nc is not None and self._nc.is_connected

    async def start(self):
        if not self.enabled:
            logger.info("NATS syncer disabled (no URL or nats-py not installed)")
            return

        try:
            self._nc = await nats.connect(self.nats_url)
            self._js = self._nc.jetstream()

            # Ensure the tasks stream exists.
            try:
                await self._js.add_stream(
                    name="TASKS",
                    subjects=["tasks.>"],
                    retention="limits",
                    max_msgs=10000,
                )
            except Exception:
                # Stream may already exist
                pass

            # Subscribe to task events from other nodes.
            self._sub = await self._js.subscribe(
                "tasks.>",
                durable=f"syncer-{self.node_id}",
                config=ConsumerConfig(
                    deliver_policy="all",
                    ack_policy="explicit",
                    ack_wait=30,
                    max_deliver=5,
                ),
            )
            self._running = True
            asyncio.create_task(self._consume_loop())
            logger.info("NATS syncer connected to %s", self.nats_url)
        except Exception:
            logger.exception("failed to connect NATS syncer")
            self._nc = None
            self._js = None

    async def stop(self):
        self._running = False
        if self._sub:
            try:
                await self._sub.unsubscribe()
            except Exception:
                pass
        if self._nc:
            try:
                await self._nc.close()
            except Exception:
                pass
        self._nc = None
        self._js = None

    async def publish_task_created(self, task: Task):
        if not self._js:
            return
        try:
            data = {
                "event": "task.created",
                "task_id": str(task.id),
                "node_id": self.node_id,
                "type": task.type.value,
                "priority": task.priority,
                "input": task.input.model_dump(),
            }
            await self._js.publish("tasks.created", json.dumps(data).encode())
        except Exception:
            logger.warning("failed to publish task_created event", exc_info=True)

    async def publish_status_changed(self, task_id: uuid.UUID, status: TaskStatus, detail: str = ""):
        if not self._js:
            return
        try:
            data = {
                "event": "task.status_changed",
                "task_id": str(task_id),
                "node_id": self.node_id,
                "status": status.value,
                "detail": detail,
            }
            await self._js.publish("tasks.status_changed", json.dumps(data).encode())
        except Exception:
            logger.warning("failed to publish status_changed event", exc_info=True)

    async def publish_task_claimed(self, task_id: uuid.UUID, agent_id: str, node_id: str):
        if not self._js:
            return
        try:
            data = {
                "event": "task.claimed",
                "task_id": str(task_id),
                "node_id": node_id,
                "agent_id": agent_id,
            }
            await self._js.publish("tasks.claimed", json.dumps(data).encode())
        except Exception:
            logger.warning("failed to publish task_claimed event", exc_info=True)

    async def _consume_loop(self):
        prune_interval = 60.0
        last_prune = time.time()
        while self._running and self._sub:
            try:
                async for msg in self._sub.messages:
                    if not self._running:
                        break
                    try:
                        data = json.loads(msg.data.decode())
                        # Skip events from this node
                        if data.get("node_id") == self.node_id:
                            await msg.ack()
                            continue
                        # Deduplication: skip if we've seen this event recently
                        key = f"{data.get('task_id')}:{data.get('event')}:{data.get('node_id')}"
                        now = time.time()
                        if key in self._seen:
                            await msg.ack()
                            continue
                        self._seen[key] = now
                        # Prune old entries periodically
                        if now - last_prune > prune_interval:
                            cutoff = now - 30.0
                            self._seen = {k: t for k, t in self._seen.items() if t > cutoff}
                            last_prune = now
                        await self._handle_event(data)
                        await msg.ack()
                    except Exception:
                        logger.warning("failed to handle NATS event", exc_info=True)
                        try:
                            await msg.nak()
                        except Exception:
                            await msg.ack()
            except Exception:
                if self._running:
                    logger.warning("NATS consume loop error, retrying in 5s", exc_info=True)
                    await asyncio.sleep(5)

    async def _handle_event(self, data: dict):
        event_type = data.get("event", "")
        task_id_str = data.get("task_id", "")

        if event_type == "task.created":
            logger.info("remote task created: %s from node %s", task_id_str, data.get("node_id"))
            await self._handle_task_created(data)
        elif event_type == "task.status_changed":
            logger.info(
                "remote status change: %s → %s from node %s",
                task_id_str, data.get("status"), data.get("node_id"),
            )
            await self._handle_status_changed(data)
        elif event_type == "task.claimed":
            logger.info(
                "remote task claimed: %s by %s on node %s",
                task_id_str, data.get("agent_id"), data.get("node_id"),
            )
            await self._handle_task_claimed(data)

    async def _handle_task_created(self, data: dict):
        """Apply a remote task creation to the local store."""
        task_id_str = data.get("task_id", "")
        try:
            task_uuid = uuid.UUID(task_id_str)
        except ValueError:
            logger.warning("invalid UUID in task.created: %s", task_id_str)
            return

        # Skip if we already have this task locally
        existing = self.store.get_task(task_uuid)
        if existing:
            logger.info("task %s already exists locally, skipping", task_id_str)
            return

        try:
            input_data = data.get("input", {})
            task = Task(
                id=task_uuid,
                project_id=uuid.UUID(input_data["project_id"]) if input_data.get("project_id") else None,
                type=TaskType(data.get("type", "dev")),
                priority=data.get("priority", 0),
                node_id=data.get("node_id", ""),
                input=TaskInput(
                    description=input_data.get("description", ""),
                    params=input_data.get("params", {}),
                ),
            )
            self.store.create_task(task)
            logger.info("synced remote task %s to local store", task_id_str)
        except Exception:
            logger.exception("failed to apply remote task.created for %s", task_id_str)

    async def _handle_status_changed(self, data: dict):
        """Apply a remote status change to the local store."""
        task_id_str = data.get("task_id", "")
        try:
            task_uuid = uuid.UUID(task_id_str)
        except ValueError:
            logger.warning("invalid UUID in task.status_changed: %s", task_id_str)
            return

        status_str = data.get("status", "")
        try:
            new_status = TaskStatus(status_str)
        except ValueError:
            logger.warning("invalid status in task.status_changed: %s", status_str)
            return

        try:
            task = self.store.get_task(task_uuid)
            if not task:
                logger.warning("status change for unknown task %s", task_id_str)
                return
            detail = data.get("detail", "")
            self.store.update_status(task_uuid, new_status, detail)
            logger.info("synced remote status change for task %s → %s", task_id_str, status_str)
        except Exception:
            logger.exception("failed to apply remote status_changed for %s", task_id_str)

    async def _handle_task_claimed(self, data: dict):
        """Apply a remote task claim to the local store."""
        task_id_str = data.get("task_id", "")
        try:
            task_uuid = uuid.UUID(task_id_str)
        except ValueError:
            logger.warning("invalid UUID in task.claimed: %s", task_id_str)
            return

        agent_id = data.get("agent_id", "")
        remote_node_id = data.get("node_id", "")

        try:
            task = self.store.get_task(task_uuid)
            if not task:
                logger.warning("claim for unknown task %s", task_id_str)
                return
            # Only apply if the task is still pending (wasn't claimed locally in the meantime)
            if task.status != TaskStatus.PENDING:
                return
            self.store.claim_task(task_uuid, agent_id, remote_node_id)
            logger.info("synced remote claim for task %s by %s on node %s",
                         task_id_str, agent_id, remote_node_id)
        except Exception:
            logger.exception("failed to apply remote task.claimed for %s", task_id_str)

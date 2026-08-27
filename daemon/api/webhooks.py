"""Async webhook delivery dispatcher — fires HTTP POST to registered endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False


# Supported event types
ALL_EVENTS = (
    "task.created",
    "task.status_changed",
    "task.completed",
    "task.failed",
    "task.approved",
)


class WebhookDispatcher:
    """Fires webhooks asynchronously. Falls back to no-op if httpx unavailable."""

    def __init__(self, store):
        self.store = store
        self._queue: list = []

    async def dispatch(self, event: str, payload: dict, project_id: uuid.UUID):
        """Deliver webhook to all matching registered endpoints."""
        if not HTTPX_AVAILABLE:
            return

        webhooks = self.store.get_active_webhooks(project_id, event)
        if not webhooks:
            return

        for wh in webhooks:
            asyncio.create_task(self._deliver(wh, event, payload))

    async def _deliver(self, webhook, event: str, payload: dict):
        """Send one webhook with HMAC signature."""
        try:
            body = json.dumps({
                "event": event,
                "delivered_at": datetime.now(timezone.utc).isoformat() + "Z",
                "payload": payload,
            })
            signature = hmac.new(
                webhook.secret.encode(),
                body.encode(),
                hashlib.sha256,
            ).hexdigest()

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    webhook.url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Webhook-Signature": signature,
                        "X-Webhook-Event": event,
                    },
                )
            if resp.status_code >= 400:
                logger.warning("webhook %s delivered with status %d", webhook.id, resp.status_code)
        except Exception:
            logger.warning("webhook delivery failed for %s: %s", webhook.id, event)


def build_webhook_payload(event: str, task, detail: str = "") -> dict:
    """Build the payload dict for a webhook event."""
    return {
        "task_id": str(task.id),
        "project_id": str(task.project_id) if task.project_id else None,
        "task_type": task.type.value if hasattr(task, "type") else "",
        "task_status": task.status.value if hasattr(task, "status") else "",
        "detail": detail,
    }

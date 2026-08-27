"""Base agent class for all MyDevTeam Python agents."""

from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass

from .comms import UnixSocketClient
from .llm import LLMClient


@dataclass
class RetryStrategy:
    """Configures retry behavior for transient failures."""
    max_retries: int = 2
    base_delay: float = 2.0
    max_delay: float = 60.0
    jitter: bool = True


def _backoff_delay(attempt: int, strategy: RetryStrategy) -> float:
    """Compute delay with exponential backoff and optional jitter."""
    delay = min(strategy.base_delay * (2 ** attempt), strategy.max_delay)
    if strategy.jitter:
        delay *= random.uniform(0.8, 1.2)
    return delay


class BaseAgent:
    """Common foundation for all Python agent types."""

    DEFAULT_RETRY = RetryStrategy()

    def __init__(self):
        # Read config from environment variables
        self.socket_path = os.environ.get("DEVTEAM_SOCKET", "")
        self.agent_id = os.environ.get("DEVTEAM_AGENT_ID", "")
        self.agent_type = os.environ.get("DEVTEAM_AGENT_TYPE", "")
        self.project_id = os.environ.get("DEVTEAM_PROJECT_ID", "")
        self.agent_secret = os.environ.get("DEVTEAM_AGENT_SECRET", "")

        llm_config_raw = os.environ.get("DEVTEAM_LLM_CONFIG", "{}")
        self.llm_config = json.loads(llm_config_raw)

        self.client = UnixSocketClient(self.socket_path, self.agent_secret) if self.socket_path else None
        self.llm = LLMClient(self.llm_config) if self.llm_config.get("primary") else None

        self._stop_event = threading.Event()
        self._retry_strategy = getattr(self, "retry_strategy", None) or self.DEFAULT_RETRY

        # Set up logging — output goes to stdout/stderr which the manager captures to log files
        logging.basicConfig(
            level=logging.INFO,
            format=f"%(asctime)s [{self.agent_id}] %(levelname)s: %(message)s",
            datefmt="%H:%M:%S",
        )
        self.log = logging.getLogger(self.agent_id or self.agent_type)

    def run(self):
        """Main loop: send heartbeats, poll for tasks, handle them."""
        model = self.llm_config.get("primary", {}).get("model", "none")
        self.log.info("started (type=%s, project=%s, model=%s)", self.agent_type, self.project_id[:8] if self.project_id else "none", model)

        # Start heartbeat thread
        heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        heartbeat_thread.start()

        poll_empty_delay = self._retry_strategy.base_delay

        while not self._stop_event.is_set():
            task = self.claim_task()
            if task:
                task_id = task.get("id", "")
                desc = (task.get("input", {}) or {}).get("description", "")[:60]
                self.log.info("claimed task %s: %s", task_id[:8], desc)
                poll_empty_delay = self._retry_strategy.base_delay  # reset on successful claim
                try:
                    self._last_status = None
                    self.update_status(task_id, "in_progress")
                    self.handle_task_with_retries(task)
                    if self._last_status in ("blocked", "cancelled"):
                        self.log.info("task %s ended with status: %s", task_id[:8], self._last_status)
                    else:
                        self.log.info("completed task %s", task_id[:8])
                except Exception as exc:
                    self.log.error("task %s failed: %s", task_id[:8], exc)
                    traceback.print_exc()
                    try:
                        self.update_status(task_id, "failed", message=str(exc))
                    except Exception:
                        pass
            else:
                # No task available — backoff with doubling up to max_delay
                time.sleep(poll_empty_delay)
                poll_empty_delay = min(poll_empty_delay * 2, self._retry_strategy.max_delay)

    def handle_task_with_retries(self, task: dict) -> None:
        """Run handle_task with retry on transient failures."""
        attempt = 0
        strategy = self._retry_strategy
        while True:
            try:
                self.handle_task(task)
                return
            except (json.JSONDecodeError, ConnectionError, OSError) as exc:
                if attempt >= strategy.max_retries:
                    raise
                delay = _backoff_delay(attempt, strategy)
                self.log.warning("handle_task transient failure (attempt %d): %s, retrying in %.1fs", attempt, exc, delay)
                time.sleep(delay)
                attempt += 1
            except (NotImplementedError, ValueError) as exc:
                # Non-transient — don't retry
                raise

    def handle_task(self, task: dict) -> None:
        """Override in subclasses. Process a claimed task."""
        raise NotImplementedError

    def _heartbeat_loop(self):
        """Background thread sending heartbeats every 10s."""
        while not self._stop_event.is_set():
            try:
                self._api_call("/api/heartbeat", {"agent_id": self.agent_id})
            except Exception:
                pass  # Heartbeat failures are non-fatal
            self._stop_event.wait(10)

    def _api_call(self, endpoint: str, data: dict) -> dict:
        """Make HTTP POST to daemon Unix socket API. Returns JSON response."""
        if not self.client:
            return {}
        return self.client.post(endpoint, data)

    def claim_task(self) -> dict | None:
        """Claim next available task for this agent type (scoped to project if set)."""
        payload = {"agent_id": self.agent_id, "agent_type": self.agent_type}
        if self.project_id:
            payload["project_id"] = self.project_id
        result = self._api_call("/api/task/claim", payload)
        if result and result.get("task"):
            return result["task"]
        return None

    def update_status(self, task_id: str, status: str, message: str = ""):
        """Update task status via daemon API."""
        self._last_status = status
        self._api_call(
            "/api/task/status",
            {"task_id": task_id, "status": status, "detail": message},
        )

    def input_value(self, task: dict, key: str, default: str = "") -> str:
        """Read task input from either flattened JSON or TaskInput.params."""
        task_input = task.get("input", {}) or {}
        params = task_input.get("params", {}) or {}

        value = task_input.get(key)
        if value not in (None, ""):
            return value

        value = params.get(key)
        if value not in (None, ""):
            return value

        if key == "content":
            return task_input.get("description", default) or default

        return default

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
            pass  # Non-fatal

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
            return task

    def git_clone(self, task_id: str, repo_url: str) -> str:
        """Clone repo for task, returns workspace path."""
        workspace = f"/tmp/devteam/{task_id}"
        subprocess.run(
            ["git", "clone", repo_url, workspace],
            check=True,
            capture_output=True,
        )
        return workspace

    def git_branch(self, workspace: str, branch: str):
        """Create a new branch in workspace."""
        subprocess.run(
            ["git", "checkout", "-b", branch],
            cwd=workspace,
            check=True,
            capture_output=True,
        )

    def git_commit(self, workspace: str, message: str):
        """Stage and commit all changes."""
        subprocess.run(
            ["git", "add", "-A"],
            cwd=workspace,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=workspace,
            check=True,
            capture_output=True,
        )

    def git_push(self, workspace: str, branch: str):
        """Push branch to remote."""
        subprocess.run(
            ["git", "push", "origin", branch],
            cwd=workspace,
            check=True,
            capture_output=True,
        )

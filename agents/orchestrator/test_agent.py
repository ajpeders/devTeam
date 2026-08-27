"""Tests for orchestrator agent task decomposition and monitoring."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock, patch

import httpx
import pytest

from agents.orchestrator.agent import OrchestratorAgent
from daemon.api.server import DaemonServer
from daemon.nats.sync import TaskSyncer
from daemon.tasks.models import User
from daemon.tasks.store import TaskStore

ADMIN_KEY = "test-admin-key-12345"
AGENT_KEY = "test-agent-key-67890"

@pytest.fixture()
def store(tmp_path):
    s = TaskStore(str(tmp_path / "test.db"))
    admin = User(name="admin", api_key=ADMIN_KEY, is_admin=True)
    s.create_user(admin)
    yield s
    s.close()

def _h() -> dict:
    return {"X-Api-Key": ADMIN_KEY}


def _agent_h() -> dict:
    return {"X-Agent-Key": AGENT_KEY}


def _run_client(store: TaskStore, scenario):
    async def _run():
        class _Client:
            def __init__(self, client: httpx.AsyncClient):
                self._client = client

            async def request(self, method: str, url: str, **kwargs):
                task = asyncio.create_task(self._client.request(method, url, **kwargs))
                await asyncio.sleep(0.01)
                return await task

            async def post(self, url: str, **kwargs):
                return await self.request("POST", url, **kwargs)

            async def get(self, url: str, **kwargs):
                return await self.request("GET", url, **kwargs)

            async def patch(self, url: str, **kwargs):
                return await self.request("PATCH", url, **kwargs)

            async def delete(self, url: str, **kwargs):
                return await self.request("DELETE", url, **kwargs)

        syncer = TaskSyncer(None, store, "test-node")
        srv = DaemonServer(
            store=store, syncer=syncer, workspace=None, node_id="test-node",
            agent_secret=AGENT_KEY,
        )
        transport = httpx.ASGITransport(app=srv.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as raw_client:
            return await scenario(_Client(raw_client))

    return asyncio.run(_run())


def _make_agent() -> OrchestratorAgent:
    with patch.dict(os.environ, {
        "DEVTEAM_SOCKET": "",
        "DEVTEAM_AGENT_ID": "orch-1",
        "DEVTEAM_AGENT_TYPE": "orchestrator",
        "DEVTEAM_LLM_CONFIG": "{}",
    }):
        agent = OrchestratorAgent()
    agent.client = MagicMock()
    agent.llm = MagicMock()
    return agent

class TestOrchestratorTaskFlow:
    def test_orchestrator_task_type_claimable(self, store):
        async def scenario(client):
            project_id = (await client.post("/api/project/create", headers=_h(), json={
                "name": "orch-test", "repo_url": "file:///tmp/repo",
            })).json()["project_id"]
            resp = await client.post("/api/task/create_internal", headers=_agent_h(), json={
                "project_id": project_id, "type": "orchestrator",
                "description": "Build auth system",
            })
            assert resp.status_code == 200
            resp = await client.post("/api/task/claim", headers=_agent_h(), json={
                "agent_id": "orchestrator-0", "agent_type": "orchestrator",
                "project_id": project_id,
            })
            assert resp.json()["found"] is True
            assert resp.json()["task"]["type"] == "orchestrator"

        _run_client(store, scenario)

    def test_child_tasks_linked_by_parent_id(self, store):
        async def scenario(client):
            project_id = (await client.post("/api/project/create", headers=_h(), json={
                "name": "parent-test", "repo_url": "file:///tmp/repo",
            })).json()["project_id"]
            parent_id = (await client.post("/api/task/create_internal", headers=_agent_h(), json={
                "project_id": project_id, "type": "orchestrator",
                "description": "Parent task",
            })).json()["task_id"]
            child_id = (await client.post("/api/task/create_internal", headers=_agent_h(), json={
                "project_id": project_id, "parent_id": parent_id,
                "type": "dev", "description": "Child task",
            })).json()["task_id"]
            children = (await client.post("/api/task/list_internal", headers=_agent_h(), json={
                "parent_id": parent_id,
            })).json()["tasks"]
            assert len(children) == 1
            assert children[0]["id"] == child_id
            assert children[0]["parent_id"] == parent_id

        _run_client(store, scenario)

    def test_dev_pool_state_endpoint(self, store):
        async def scenario(client):
            await client.post("/api/dev/create", headers=_h(), json={
                "model": "qwen3:8b", "provider": "ollama",
            })
            resp = await client.post("/api/dev/pool_state", headers=_agent_h(), json={})
            assert resp.status_code == 200
            assert len(resp.json()["slots"]) == 1
            assert resp.json()["slots"][0]["model"] == "qwen3:8b"

        _run_client(store, scenario)

    def test_edit_internal_bumps_revision(self, store):
        async def scenario(client):
            project_id = (await client.post("/api/project/create", headers=_h(), json={
                "name": "edit-int", "repo_url": "file:///tmp/repo",
            })).json()["project_id"]
            task_id = (await client.post("/api/task/create_internal", headers=_agent_h(), json={
                "project_id": project_id, "type": "dev", "description": "Original",
            })).json()["task_id"]
            await client.post("/api/task/edit_internal", headers=_agent_h(), json={
                "task_id": task_id, "params": {"sibling_context": "some info"},
            })
            task = (await client.post("/api/task/get_internal", headers=_agent_h(), json={
                "task_id": task_id,
            })).json()["task"]
            assert task["revision"] == 1

        _run_client(store, scenario)


class TestOrchestratorCoordination:
    def test_relay_context_deduplicates_messages(self):
        agent = _make_agent()
        agent._api_call = MagicMock(return_value={"ok": True})

        completed_child = {
            "id": "child-1",
            "history": [{"status": "completed", "detail": "branch:devteam/abc123"}],
        }
        all_children = [
            completed_child,
            {
                "id": "child-2",
                "status": "pending",
                "input": {"params": {"sibling_context": "Context from sibling task: branch:devteam/abc123"}},
            },
        ]

        agent._relay_context(completed_child, all_children)

        agent._api_call.assert_not_called()

    def test_monitor_children_keeps_parent_open_after_answering_escalation(self):
        agent = _make_agent()
        agent._answer_escalation = MagicMock(return_value="Use the auth module")
        agent._get_children = MagicMock(return_value=[
            {
                "id": "child-1",
                "status": "blocked",
                "input": {"params": {"escalate": True, "escalate_question": "Which module owns auth?"}},
            },
            {
                "id": "child-2",
                "status": "completed",
                "input": {"params": {}},
                "history": [],
            },
        ])
        agent.update_status = MagicMock()

        def _api_call(endpoint: str, data: dict) -> dict:
            if endpoint == "/api/task/status" and data.get("task_id") == "child-1":
                agent._stop_event.set()
            return {"ok": True}

        agent._api_call = MagicMock(side_effect=_api_call)

        with patch("agents.orchestrator.agent.time.sleep", return_value=None):
            agent._monitor_children("parent-1", "project-1", ["child-1", "child-2"])

        agent.update_status.assert_not_called()

    def test_retry_child_requeues_same_task_with_retry_context(self):
        agent = _make_agent()
        agent._api_call = MagicMock(return_value={"ok": True})

        failed_child = {
            "id": "child-1",
            "input": {
                "description": "Implement auth",
                "params": {"retry_count": "1"},
            },
            "history": [
                {"status": "failed", "detail": "tests failed"},
            ],
        }

        agent._retry_child(failed_child)

        assert agent._api_call.call_args_list[0][0] == (
            "/api/task/status",
            {
                "task_id": "child-1",
                "status": "pending",
                "message": "retrying child task",
            },
        )
        assert agent._api_call.call_args_list[1][0] == (
            "/api/task/edit_internal",
            {
                "task_id": "child-1",
                "params": {
                    "retry_count": "2",
                    "retry_context": "Previous attempt failed: tests failed",
                },
            },
        )

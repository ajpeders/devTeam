"""Tests for the Dev agent."""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from agents.base.agent import BaseAgent
from agents.dev.agent import DevAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent() -> DevAgent:
    """Create a DevAgent with mocked dependencies."""
    with patch.dict(os.environ, {
        "DEVTEAM_SOCKET": "",
        "DEVTEAM_AGENT_ID": "test-agent",
        "DEVTEAM_AGENT_TYPE": "dev",
        "DEVTEAM_LLM_CONFIG": "{}",
    }):
        agent = DevAgent()
    agent.llm = MagicMock()
    agent.client = MagicMock()
    return agent


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDevAgentIsBaseAgent:
    def test_subclass(self):
        assert issubclass(DevAgent, BaseAgent)

    def test_instance(self):
        agent = _make_agent()
        assert isinstance(agent, BaseAgent)


class TestGetFileTree:
    def test_lists_files(self):
        agent = _make_agent()
        with tempfile.TemporaryDirectory() as tmp:
            # Create some files
            os.makedirs(os.path.join(tmp, "src"))
            open(os.path.join(tmp, "README.md"), "w").close()
            open(os.path.join(tmp, "src", "main.py"), "w").close()
            # Hidden dir should be skipped
            os.makedirs(os.path.join(tmp, ".git"))
            open(os.path.join(tmp, ".git", "config"), "w").close()

            result = agent._get_file_tree(tmp)
            assert "README.md" in result
            assert "src/main.py" in result
            assert ".git" not in result

    def test_empty_dir(self):
        agent = _make_agent()
        with tempfile.TemporaryDirectory() as tmp:
            result = agent._get_file_tree(tmp)
            assert result == ""


class TestPlanChanges:
    def test_calls_llm_with_correct_structure(self):
        agent = _make_agent()
        plan_json = json.dumps({
            "files": [{"path": "src/app.py", "action": "create", "description": "main app"}]
        })
        agent.llm.chat.return_value = plan_json

        with tempfile.TemporaryDirectory() as tmp:
            result = agent._plan_changes("Build a web server", tmp)

        assert "files" in result
        assert result["files"][0]["path"] == "src/app.py"

        # Verify LLM was called with system + user messages
        call_args = agent.llm.chat.call_args[0][0]
        assert len(call_args) == 2
        assert call_args[0]["role"] == "system"
        assert call_args[1]["role"] == "user"
        assert "Build a web server" in call_args[1]["content"]

    def test_extracts_json_from_surrounding_text(self):
        agent = _make_agent()
        agent.llm.chat.return_value = (
            'Here is my plan:\n{"files": [{"path": "x.py", "action": "create", '
            '"description": "file"}]}\nDone!'
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = agent._plan_changes("task", tmp)
        assert result["files"][0]["path"] == "x.py"

    def test_returns_empty_on_bad_json(self):
        agent = _make_agent()
        agent.llm.chat.return_value = "no json here"
        with tempfile.TemporaryDirectory() as tmp:
            result = agent._plan_changes("task", tmp)
        assert result == {"files": []}

    def test_includes_orchestrator_guidance_in_prompt(self):
        agent = _make_agent()
        agent.llm.chat.return_value = '{"files": []}'

        with tempfile.TemporaryDirectory() as tmp:
            agent._plan_changes("task", tmp, escalate_response="Use the existing auth service")

        call_args = agent.llm.chat.call_args[0][0]
        assert "Orchestrator guidance: Use the existing auth service" in call_args[1]["content"]

    def test_includes_sibling_context_in_prompt(self):
        agent = _make_agent()
        agent.llm.chat.return_value = '{"files": []}'

        with tempfile.TemporaryDirectory() as tmp:
            agent._plan_changes("task", tmp, sibling_context="branch:devteam/abc123 added auth module")

        call_args = agent.llm.chat.call_args[0][0]
        assert "branch:devteam/abc123 added auth module" in call_args[1]["content"]

    def test_omits_sibling_context_when_empty(self):
        agent = _make_agent()
        agent.llm.chat.return_value = '{"files": []}'

        with tempfile.TemporaryDirectory() as tmp:
            agent._plan_changes("task", tmp, sibling_context="")

        call_args = agent.llm.chat.call_args[0][0]
        assert "Context from completed sibling tasks" not in call_args[1]["content"]


class TestApplyChanges:
    def test_creates_files_from_plan(self):
        agent = _make_agent()
        agent.llm.chat.return_value = "print('hello world')"

        plan = {
            "files": [
                {"path": "src/hello.py", "action": "create", "description": "greeting script"}
            ]
        }

        with tempfile.TemporaryDirectory() as tmp:
            agent._apply_changes(plan, tmp)

            created = os.path.join(tmp, "src", "hello.py")
            assert os.path.exists(created)
            with open(created) as f:
                assert f.read() == "print('hello world')"

    def test_modifies_existing_file(self):
        agent = _make_agent()
        agent.llm.chat.return_value = "print('updated')"

        with tempfile.TemporaryDirectory() as tmp:
            existing = os.path.join(tmp, "app.py")
            with open(existing, "w") as f:
                f.write("print('old')")

            plan = {
                "files": [
                    {"path": "app.py", "action": "modify", "description": "update app"}
                ]
            }
            agent._apply_changes(plan, tmp)

            with open(existing) as f:
                assert f.read() == "print('updated')"

            # Verify prompt included existing content
            call_args = agent.llm.chat.call_args[0][0]
            assert "Existing content:" in call_args[1]["content"]
            assert "print('old')" in call_args[1]["content"]

    def test_rejects_path_traversal(self):
        agent = _make_agent()
        agent.llm.chat.return_value = "print('pwned')"

        plan = {
            "files": [
                {"path": "../../etc/evil.py", "action": "create", "description": "escape"},
                {"path": "safe.py", "action": "create", "description": "legit file"},
            ]
        }

        with tempfile.TemporaryDirectory() as tmp:
            agent._apply_changes(plan, tmp)

            # Traversal path must NOT be written
            assert not os.path.exists(os.path.join(tmp, "..", "..", "etc", "evil.py"))
            # Safe path should still be written
            assert os.path.exists(os.path.join(tmp, "safe.py"))
            # LLM should only be called once (for the safe file)
            assert agent.llm.chat.call_count == 1


class TestHandleTask:
    def test_full_flow(self):
        agent = _make_agent()

        task = {
            "id": "abcdef1234567890",
            "input": {
                "repo_url": "https://github.com/test/repo.git",
                "content": "Add a hello world endpoint",
            },
        }

        plan_json = json.dumps({
            "files": [{"path": "app.py", "action": "create", "description": "endpoint"}]
        })

        with tempfile.TemporaryDirectory() as tmp:
            agent.git_clone = MagicMock(return_value=tmp)
            agent.git_branch = MagicMock()
            agent.git_commit = MagicMock()
            agent.git_push = MagicMock()
            agent.update_status = MagicMock()
            agent._refresh_llm_config = MagicMock()
            agent._check_task_revision = MagicMock(return_value=task)

            # First LLM call = plan, second = code generation
            agent.llm.chat.side_effect = [plan_json, "print('hello')"]

            agent.handle_task(task)

            # Verify git operations
            agent.git_clone.assert_called_once_with("abcdef1234567890", "https://github.com/test/repo.git")
            agent.git_branch.assert_called_once_with(tmp, "devteam/abcdef12")
            agent.git_commit.assert_called_once_with(tmp, "feat: Add a hello world endpoint")
            agent.git_push.assert_called_once_with(tmp, "devteam/abcdef12")
            agent.update_status.assert_called_once_with(
                "abcdef1234567890", "completed", message="branch:devteam/abcdef12"
            )

            # Verify file was created
            assert os.path.exists(os.path.join(tmp, "app.py"))


class TestSiblingCoordination:
    def test_get_siblings_excludes_current_task(self):
        agent = _make_agent()
        agent._api_call = MagicMock(return_value={
            "tasks": [
                {"id": "task-self", "status": "in_progress"},
                {"id": "task-other", "status": "pending"},
            ]
        })

        result = agent._get_siblings("task-self", "parent-1")

        assert result == [{"id": "task-other", "status": "pending"}]

    def test_notify_siblings_skips_current_task(self):
        agent = _make_agent()
        agent._api_call = MagicMock(side_effect=[
            {
                "tasks": [
                    {"id": "task-self", "status": "in_progress", "input": {"params": {}}},
                    {"id": "task-other", "status": "pending", "input": {"params": {}}},
                ]
            },
            {"ok": True},
        ])

        agent._notify_siblings("task-self", "parent-1", "finished branch")

        assert agent._api_call.call_args_list[1][0] == (
            "/api/task/edit_internal",
            {"task_id": "task-other", "params": {"sibling_context": "finished branch"}},
        )

    def test_escalate_to_orchestrator_includes_task_id(self):
        agent = _make_agent()
        agent._api_call = MagicMock(return_value={"ok": True})

        agent._escalate_to_orchestrator("task-123", "parent-1", "which module owns auth?")

        agent._api_call.assert_called_once_with(
            "/api/task/edit_internal",
            {
                "task_id": "task-123",
                "params": {
                    "escalate": True,
                    "escalate_question": "which module owns auth?",
                },
            },
        )

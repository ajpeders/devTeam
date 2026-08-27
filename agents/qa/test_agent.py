"""Tests for the QA agent."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import MagicMock, patch, call

import pytest

from agents.base.agent import BaseAgent
from agents.qa.agent import QAAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent() -> QAAgent:
    """Create a QAAgent with mocked dependencies."""
    with patch.dict(os.environ, {
        "DEVTEAM_SOCKET": "",
        "DEVTEAM_AGENT_ID": "test-agent",
        "DEVTEAM_AGENT_TYPE": "qa",
        "DEVTEAM_LLM_CONFIG": "{}",
    }):
        agent = QAAgent()
    agent.llm = MagicMock()
    agent.client = MagicMock()
    return agent


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestQAAgentIsBaseAgent:
    def test_subclass(self):
        assert issubclass(QAAgent, BaseAgent)

    def test_instance(self):
        agent = _make_agent()
        assert isinstance(agent, BaseAgent)


class TestDetectTestRunner:
    def test_detects_pytest_from_pytest_ini(self):
        agent = _make_agent()
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "pytest.ini"), "w").close()
            assert agent._detect_test_runner(tmp) == "pytest"

    def test_detects_pytest_from_pyproject_toml(self):
        agent = _make_agent()
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "pyproject.toml"), "w").close()
            assert agent._detect_test_runner(tmp) == "pytest"

    def test_detects_go(self):
        agent = _make_agent()
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "go.mod"), "w").close()
            assert agent._detect_test_runner(tmp) == "go"

    def test_detects_npm(self):
        agent = _make_agent()
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "package.json"), "w").close()
            assert agent._detect_test_runner(tmp) == "npm"

    def test_detects_cargo(self):
        agent = _make_agent()
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "Cargo.toml"), "w").close()
            assert agent._detect_test_runner(tmp) == "cargo"

    def test_defaults_to_pytest(self):
        agent = _make_agent()
        with tempfile.TemporaryDirectory() as tmp:
            assert agent._detect_test_runner(tmp) == "pytest"


class TestRunTests:
    @patch("agents.qa.agent.subprocess.run")
    def test_runs_pytest_command(self, mock_run):
        agent = _make_agent()
        mock_run.return_value = MagicMock(
            returncode=0, stdout="2 passed", stderr=""
        )
        result = agent._run_tests("/workspace", "pytest")
        mock_run.assert_called_once_with(
            [sys.executable, "-m", "pytest", "-v", "--tb=short"],
            cwd="/workspace",
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result["passed"] is True
        assert "2 passed" in result["summary"]

    @patch("agents.qa.agent.subprocess.run")
    def test_runs_go_command(self, mock_run):
        agent = _make_agent()
        mock_run.return_value = MagicMock(
            returncode=0, stdout="ok", stderr=""
        )
        result = agent._run_tests("/workspace", "go")
        mock_run.assert_called_once_with(
            ["go", "test", "./...", "-v"],
            cwd="/workspace",
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result["passed"] is True

    @patch("agents.qa.agent.subprocess.run")
    def test_runs_npm_command(self, mock_run):
        agent = _make_agent()
        mock_run.return_value = MagicMock(
            returncode=0, stdout="ok", stderr=""
        )
        agent._run_tests("/workspace", "npm")
        mock_run.assert_called_once_with(
            ["npm", "test"],
            cwd="/workspace",
            capture_output=True,
            text=True,
            timeout=300,
        )

    @patch("agents.qa.agent.subprocess.run")
    def test_runs_cargo_command(self, mock_run):
        agent = _make_agent()
        mock_run.return_value = MagicMock(
            returncode=0, stdout="ok", stderr=""
        )
        agent._run_tests("/workspace", "cargo")
        mock_run.assert_called_once_with(
            ["cargo", "test"],
            cwd="/workspace",
            capture_output=True,
            text=True,
            timeout=300,
        )

    @patch("agents.qa.agent.subprocess.run")
    def test_reports_failure(self, mock_run):
        agent = _make_agent()
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="FAILED"
        )
        result = agent._run_tests("/workspace", "pytest")
        assert result["passed"] is False
        assert result["returncode"] == 1

    @patch("agents.qa.agent.subprocess.run")
    def test_handles_timeout(self, mock_run):
        agent = _make_agent()
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=300)
        result = agent._run_tests("/workspace", "pytest")
        assert result["passed"] is False
        assert "timed out" in result["summary"]


class TestGenerateTests:
    def test_calls_llm_with_correct_structure(self):
        agent = _make_agent()
        agent.llm.chat.return_value = json.dumps({
            "test_files": [{"path": "tests/test_foo.py", "content": "def test_foo(): pass"}]
        })
        result = agent._generate_tests("diff content", "Add login", "/ws", "pytest")

        assert len(result) == 1
        assert result[0]["path"] == "tests/test_foo.py"

        call_args = agent.llm.chat.call_args[0][0]
        assert len(call_args) == 2
        assert call_args[0]["role"] == "system"
        assert "pytest" in call_args[0]["content"]
        assert call_args[1]["role"] == "user"
        assert "Add login" in call_args[1]["content"]
        assert "diff content" in call_args[1]["content"]

    def test_extracts_json_from_surrounding_text(self):
        agent = _make_agent()
        agent.llm.chat.return_value = (
            'Here are tests:\n{"test_files": [{"path": "t.py", "content": "pass"}]}\nDone'
        )
        result = agent._generate_tests("diff", "task", "/ws", "pytest")
        assert result[0]["path"] == "t.py"

    def test_returns_empty_on_bad_json(self):
        agent = _make_agent()
        agent.llm.chat.return_value = "no json here at all"
        result = agent._generate_tests("diff", "task", "/ws", "pytest")
        assert result == []

    def test_returns_empty_on_malformed_extracted_json(self):
        agent = _make_agent()
        agent.llm.chat.return_value = (
            'Here are tests:\n{"test_files": [{"path": "t.py" "content": "pass"}]}\nDone'
        )
        result = agent._generate_tests("diff", "task", "/ws", "pytest")
        assert result == []

    def test_returns_empty_without_llm(self):
        agent = _make_agent()
        agent.llm = None
        result = agent._generate_tests("diff", "task", "/ws", "pytest")
        assert result == []


class TestWriteTests:
    def test_creates_files_in_correct_locations(self):
        agent = _make_agent()
        with tempfile.TemporaryDirectory() as tmp:
            test_files = [
                {"path": "tests/test_a.py", "content": "def test_a(): pass"},
                {"path": "tests/sub/test_b.py", "content": "def test_b(): pass"},
            ]
            agent._write_tests(test_files, tmp)

            path_a = os.path.join(tmp, "tests", "test_a.py")
            path_b = os.path.join(tmp, "tests", "sub", "test_b.py")
            assert os.path.exists(path_a)
            assert os.path.exists(path_b)
            with open(path_a) as f:
                assert f.read() == "def test_a(): pass"
            with open(path_b) as f:
                assert f.read() == "def test_b(): pass"


class TestHandleTask:
    def _make_task(self):
        return {
            "id": "task123",
            "input": {
                "repo_url": "https://github.com/test/repo.git",
                "branch": "feature-branch",
                "content": "Add user authentication",
            },
        }

    @patch("agents.qa.agent.subprocess.run")
    def test_reports_passed_when_tests_succeed(self, mock_subprocess):
        agent = _make_agent()
        task = self._make_task()

        with tempfile.TemporaryDirectory() as tmp:
            agent.git_clone = MagicMock(return_value=tmp)
            agent.git_commit = MagicMock()
            agent.git_push = MagicMock()
            agent.update_status = MagicMock()

            # subprocess.run for checkout and diff
            mock_subprocess.return_value = MagicMock(
                returncode=0, stdout="all passed", stderr=""
            )

            # LLM generates tests
            agent.llm.chat.return_value = json.dumps({
                "test_files": [{"path": "tests/test_auth.py", "content": "def test_auth(): pass"}]
            })

            agent.handle_task(task)

            agent.update_status.assert_called_once()
            status_call = agent.update_status.call_args
            assert status_call[0][1] == "completed"
            assert "tests_passed" in status_call[1].get("message", "") or "tests_passed" in status_call[0][2] if len(status_call[0]) > 2 else "tests_passed" in status_call[1].get("message", "")

    @patch("agents.qa.agent.subprocess.run")
    def test_reports_failed_when_tests_fail(self, mock_subprocess):
        agent = _make_agent()
        task = self._make_task()

        with tempfile.TemporaryDirectory() as tmp:
            agent.git_clone = MagicMock(return_value=tmp)
            agent.git_commit = MagicMock()
            agent.git_push = MagicMock()
            agent.update_status = MagicMock()

            # Checkout succeeds, but test runs fail
            def subprocess_side_effect(*args, **kwargs):
                cmd = args[0]
                if cmd[0] == "git":
                    return MagicMock(returncode=0, stdout="diff output", stderr="")
                # Test runs fail
                return MagicMock(returncode=1, stdout="", stderr="FAILED")

            mock_subprocess.side_effect = subprocess_side_effect

            # LLM generates tests
            agent.llm.chat.return_value = json.dumps({
                "test_files": [{"path": "tests/test_auth.py", "content": "def test_auth(): assert False"}]
            })

            agent.handle_task(task)

            agent.update_status.assert_called_once()
            status_call = agent.update_status.call_args
            assert status_call[0][1] == "failed"
            assert "tests_failed" in (status_call[0][2] if len(status_call[0]) > 2 else status_call[1].get("message", ""))

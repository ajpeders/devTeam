"""Tests for the agent base classes."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from agents.base.agent import BaseAgent
from agents.base.comms import UnixSocketClient
from agents.base.llm import LLMClient


class TestLLMClient:
    def test_build_model_string_ollama(self):
        config = {"primary": {"provider": "ollama", "model": "deepseek-coder-v3"}}
        client = LLMClient(config)
        assert client._build_model_string(config["primary"]) == "ollama/deepseek-coder-v3"

    def test_build_model_string_anthropic(self):
        config = {"primary": {"provider": "anthropic", "model": "claude-sonnet-4-6"}}
        client = LLMClient(config)
        assert client._build_model_string(config["primary"]) == "anthropic/claude-sonnet-4-6"

    def test_build_model_string_openai(self):
        config = {"primary": {"provider": "openai", "model": "gpt-4o"}}
        client = LLMClient(config)
        assert client._build_model_string(config["primary"]) == "openai/gpt-4o"

    def test_fallback_stored(self):
        config = {
            "primary": {"provider": "ollama", "model": "deepseek-coder-v3"},
            "fallback": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        }
        client = LLMClient(config)
        assert client.fallback == config["fallback"]

    def test_no_fallback(self):
        config = {"primary": {"provider": "ollama", "model": "deepseek-coder-v3"}}
        client = LLMClient(config)
        assert client.fallback is None

    def test_timeout_default(self):
        config = {"primary": {"provider": "ollama", "model": "x"}}
        client = LLMClient(config)
        assert client.timeout == 30

    def test_timeout_custom(self):
        config = {"primary": {"provider": "ollama", "model": "x"}, "timeout": 60}
        client = LLMClient(config)
        assert client.timeout == 60


class TestUnixSocketClient:
    def test_instantiation(self):
        client = UnixSocketClient("/tmp/test.sock")
        assert client.address == "/tmp/test.sock"

    def test_different_path(self):
        client = UnixSocketClient("/var/run/devteam.sock")
        assert client.address == "/var/run/devteam.sock"


class TestBaseAgent:
    def test_reads_env_vars(self):
        env = {
            "DEVTEAM_SOCKET": "/tmp/devteam.sock",
            "DEVTEAM_AGENT_ID": "agent-42",
            "DEVTEAM_AGENT_TYPE": "dev",
            "DEVTEAM_LLM_CONFIG": json.dumps(
                {"primary": {"provider": "ollama", "model": "deepseek-coder-v3"}}
            ),
        }
        with patch.dict(os.environ, env, clear=False):
            agent = BaseAgent()
            assert agent.socket_path == "/tmp/devteam.sock"
            assert agent.agent_id == "agent-42"
            assert agent.agent_type == "dev"
            assert agent.llm_config["primary"]["provider"] == "ollama"

    def test_handle_task_raises(self):
        with patch.dict(os.environ, {}, clear=False):
            agent = BaseAgent()
            with pytest.raises(NotImplementedError):
                agent.handle_task({"id": "task-1"})

    def test_defaults_without_env(self):
        env_keys = [
            "DEVTEAM_SOCKET",
            "DEVTEAM_AGENT_ID",
            "DEVTEAM_AGENT_TYPE",
            "DEVTEAM_LLM_CONFIG",
        ]
        cleaned = {k: v for k, v in os.environ.items() if k not in env_keys}
        with patch.dict(os.environ, cleaned, clear=True):
            agent = BaseAgent()
            assert agent.socket_path == ""
            assert agent.agent_id == ""
            assert agent.agent_type == ""
            assert agent.client is None
            assert agent.llm is None

    def test_claim_task_no_client(self):
        with patch.dict(os.environ, {}, clear=False):
            agent = BaseAgent()
            agent.client = None
            result = agent.claim_task()
            assert result is None

    def test_claim_task_includes_agent_identity(self):
        env = {
            "DEVTEAM_SOCKET": "/tmp/devteam.sock",
            "DEVTEAM_AGENT_ID": "dev-7",
            "DEVTEAM_AGENT_TYPE": "dev",
            "DEVTEAM_LLM_CONFIG": "{}",
        }
        with patch.dict(os.environ, env, clear=False):
            agent = BaseAgent()
        agent._api_call = MagicMock(return_value={"task": {"id": "task-1"}})

        assert agent.claim_task() == {"id": "task-1"}
        agent._api_call.assert_called_once_with(
            "/api/task/claim",
            {"agent_id": "dev-7", "agent_type": "dev"},
        )

    def test_claim_task_includes_project_id(self):
        env = {
            "DEVTEAM_SOCKET": "/tmp/devteam.sock",
            "DEVTEAM_AGENT_ID": "dev-0",
            "DEVTEAM_AGENT_TYPE": "dev",
            "DEVTEAM_LLM_CONFIG": "{}",
            "DEVTEAM_PROJECT_ID": "proj-abc",
        }
        with patch.dict(os.environ, env, clear=False):
            agent = BaseAgent()
        agent._api_call = MagicMock(return_value={"task": {"id": "task-2"}})

        assert agent.claim_task() == {"id": "task-2"}
        agent._api_call.assert_called_once_with(
            "/api/task/claim",
            {"agent_id": "dev-0", "agent_type": "dev", "project_id": "proj-abc"},
        )

    def test_update_status_sends_detail_field(self):
        with patch.dict(os.environ, {}, clear=False):
            agent = BaseAgent()
        agent._api_call = MagicMock(return_value={"ok": True})

        agent.update_status("task-1", "completed", message="done")
        agent._api_call.assert_called_once_with(
            "/api/task/status",
            {"task_id": "task-1", "status": "completed", "detail": "done"},
        )

    def test_input_value_reads_params_and_description(self):
        with patch.dict(os.environ, {}, clear=False):
            agent = BaseAgent()

        task = {
            "input": {
                "description": "Write a hello script",
                "params": {"repo_url": "file:///tmp/repo"},
            }
        }

        assert agent.input_value(task, "repo_url") == "file:///tmp/repo"
        assert agent.input_value(task, "content") == "Write a hello script"
        assert agent.input_value(task, "branch", default="main") == "main"

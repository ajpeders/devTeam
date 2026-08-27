"""Tests for the Deploy agent."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch, call

import pytest

from agents.base.agent import BaseAgent
from agents.deploy.agent import DeployAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(**env_overrides) -> DeployAgent:
    """Create a DeployAgent with mocked dependencies."""
    env = {
        "DEVTEAM_SOCKET": "",
        "DEVTEAM_AGENT_ID": "test-agent",
        "DEVTEAM_AGENT_TYPE": "deploy",
        "DEVTEAM_LLM_CONFIG": "{}",
        "DEVTEAM_DOCKER_REGISTRY": "",
        "DEVTEAM_DEPLOY_METHOD": "docker-compose",
        "DEVTEAM_DEPLOY_TARGET": "",
    }
    env.update(env_overrides)
    with patch.dict(os.environ, env, clear=False):
        agent = DeployAgent()
    agent.llm = MagicMock()
    agent.client = MagicMock()
    return agent


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDeployAgentIsBaseAgent:
    def test_subclass(self):
        assert issubclass(DeployAgent, BaseAgent)

    def test_instance(self):
        agent = _make_agent()
        assert isinstance(agent, BaseAgent)


class TestBuildImage:
    @patch("agents.deploy.agent.subprocess.run")
    def test_constructs_correct_docker_command(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        agent = _make_agent()

        tag = agent._build_image("/tmp/devteam/myproject", "abcdef1234567890")

        mock_run.assert_called_once_with(
            ["docker", "build", "-t", tag, "."],
            cwd="/tmp/devteam/myproject",
            capture_output=True,
            text=True,
        )

    @patch("agents.deploy.agent.subprocess.run")
    def test_with_registry_prefixes_tag(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        agent = _make_agent(DEVTEAM_DOCKER_REGISTRY="registry.example.com")

        tag = agent._build_image("/tmp/devteam/myproject", "abcdef1234567890")

        assert tag == "registry.example.com/myproject:abcdef12"

    @patch("agents.deploy.agent.subprocess.run")
    def test_without_registry_uses_plain_tag(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        agent = _make_agent(DEVTEAM_DOCKER_REGISTRY="")

        tag = agent._build_image("/tmp/devteam/myproject", "abcdef1234567890")

        assert tag == "myproject:abcdef12"

    @patch("agents.deploy.agent.subprocess.run")
    def test_build_failure_raises(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="no Dockerfile")
        agent = _make_agent()

        with pytest.raises(RuntimeError, match="Docker build failed"):
            agent._build_image("/tmp/devteam/myproject", "abcdef1234567890")


class TestPushImage:
    @patch("agents.deploy.agent.subprocess.run")
    def test_skips_when_no_registry(self, mock_run):
        agent = _make_agent(DEVTEAM_DOCKER_REGISTRY="")

        agent._push_image("myproject:abcdef12")

        mock_run.assert_not_called()

    @patch("agents.deploy.agent.subprocess.run")
    def test_pushes_when_registry_configured(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        agent = _make_agent(DEVTEAM_DOCKER_REGISTRY="registry.example.com")

        agent._push_image("registry.example.com/myproject:abcdef12")

        mock_run.assert_called_once_with(
            ["docker", "push", "registry.example.com/myproject:abcdef12"],
            capture_output=True,
            text=True,
        )


class TestGetDeployTarget:
    def test_parses_json_from_env(self):
        target_json = json.dumps({"name": "prod", "host": "ssh://prod.example.com", "compose_file": "prod.yml"})
        agent = _make_agent(DEVTEAM_DEPLOY_TARGET=target_json)

        target = agent._get_deploy_target()

        assert target["name"] == "prod"
        assert target["host"] == "ssh://prod.example.com"
        assert target["compose_file"] == "prod.yml"

    def test_returns_default_when_not_configured(self):
        agent = _make_agent(DEVTEAM_DEPLOY_TARGET="")

        target = agent._get_deploy_target()

        assert target["name"] == "local"
        assert target["host"] == "localhost"
        assert target["compose_file"] == "docker-compose.yml"

    def test_returns_default_on_invalid_json(self):
        agent = _make_agent(DEVTEAM_DEPLOY_TARGET="not-valid-json")

        target = agent._get_deploy_target()

        assert target["name"] == "local"


class TestDeploy:
    @patch("agents.deploy.agent.subprocess.run")
    def test_local_deploy_uses_docker_compose(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        agent = _make_agent()
        target = {"host": "localhost", "compose_file": "docker-compose.yml"}

        agent._deploy("myproject:abcdef12", target)

        mock_run.assert_called_once_with(
            ["docker", "compose", "-f", "docker-compose.yml", "up", "-d"],
            capture_output=True,
            text=True,
        )

    @patch("agents.deploy.agent.subprocess.run")
    def test_ssh_deploy_sets_docker_host(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        agent = _make_agent()
        target = {"host": "ssh://prod.example.com", "compose_file": "prod.yml"}

        agent._deploy("myproject:abcdef12", target)

        # Verify DOCKER_HOST was set in the env passed to subprocess
        args, kwargs = mock_run.call_args
        assert args[0] == ["docker", "compose", "-f", "prod.yml", "up", "-d"]
        assert kwargs["env"]["DOCKER_HOST"] == "ssh://prod.example.com"

    @patch("agents.deploy.agent.subprocess.run")
    def test_deploy_failure_raises(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="connection refused")
        agent = _make_agent()
        target = {"host": "localhost", "compose_file": "docker-compose.yml"}

        with pytest.raises(RuntimeError, match="Deploy failed"):
            agent._deploy("myproject:abcdef12", target)

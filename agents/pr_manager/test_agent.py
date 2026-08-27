"""Tests for PRManagerAgent."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch, call

import pytest

from agents.base.agent import BaseAgent


@pytest.fixture
def agent():
    """Create a PRManagerAgent with mocked dependencies."""
    with patch.dict("os.environ", {
        "DEVTEAM_SOCKET": "",
        "DEVTEAM_AGENT_ID": "pr-1",
        "DEVTEAM_AGENT_TYPE": "pr_manager",
        "DEVTEAM_LLM_CONFIG": "{}",
        "GITHUB_TOKEN": "fake-token",
    }):
        from agents.pr_manager.agent import PRManagerAgent
        a = PRManagerAgent()
        a.llm = MagicMock()
        a.client = MagicMock()
        return a


class TestPRManagerIsBaseAgent:
    def test_subclass(self, agent):
        assert isinstance(agent, BaseAgent)


class TestExtractOwnerRepo:
    def test_https_url(self, agent):
        url = "https://github.com/owner/repo.git"
        assert agent._extract_owner_repo(url) == "owner/repo"

    def test_https_url_no_git(self, agent):
        url = "https://github.com/owner/repo"
        assert agent._extract_owner_repo(url) == "owner/repo"

    def test_ssh_url(self, agent):
        url = "git@github.com:owner/repo.git"
        assert agent._extract_owner_repo(url) == "owner/repo"

    def test_non_github_url(self, agent):
        url = "https://gitlab.com/owner/repo.git"
        assert agent._extract_owner_repo(url) == ""


class TestReviewCode:
    def test_calls_llm_with_correct_structure(self, agent):
        review_json = json.dumps({
            "approved": True,
            "summary": "Looks good",
            "comments": []
        })
        agent.llm.chat.return_value = review_json

        result = agent._review_code("diff content", "Add feature X")

        agent.llm.chat.assert_called_once()
        messages = agent.llm.chat.call_args[0][0]
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert "code reviewer" in messages[0]["content"].lower()
        assert messages[1]["role"] == "user"
        assert "diff content" in messages[1]["content"]
        assert "Add feature X" in messages[1]["content"]
        assert result["approved"] is True

    def test_handles_non_json_response(self, agent):
        agent.llm.chat.return_value = 'Here is my review: {"approved": false, "summary": "bad", "comments": []} end'

        result = agent._review_code("diff", "task")
        assert result["approved"] is False
        assert result["summary"] == "bad"


class TestGetBranchDiff:
    @patch("agents.pr_manager.agent.subprocess.run")
    def test_runs_correct_git_commands(self, mock_run, agent):
        # First call: git fetch
        fetch_result = MagicMock()
        fetch_result.returncode = 0
        # Second call: git diff main
        diff_result = MagicMock()
        diff_result.stdout = "diff --git a/file.py b/file.py\n+new line"
        mock_run.side_effect = [fetch_result, diff_result]

        result = agent._get_branch_diff("/tmp/workspace", "feature-branch", "main")

        assert mock_run.call_count == 2
        # Verify fetch command
        fetch_call = mock_run.call_args_list[0]
        assert fetch_call[0][0] == ["git", "fetch", "origin", "feature-branch"]
        assert fetch_call[1]["cwd"] == "/tmp/workspace"
        # Verify diff command
        diff_call = mock_run.call_args_list[1]
        assert "origin/main...origin/feature-branch" in diff_call[0][0]
        assert result == "diff --git a/file.py b/file.py\n+new line"

    @patch("agents.pr_manager.agent.subprocess.run")
    def test_falls_back_to_master(self, mock_run, agent):
        fetch_result = MagicMock()
        fetch_result.returncode = 0
        # main diff returns empty
        main_diff = MagicMock()
        main_diff.stdout = ""
        # master diff returns content
        master_diff = MagicMock()
        master_diff.stdout = "master diff"
        mock_run.side_effect = [fetch_result, main_diff, master_diff]

        result = agent._get_branch_diff("/tmp/workspace", "feat", "main")
        assert result == "master diff"
        assert mock_run.call_count == 3


class TestHandleTaskApproved:
    @patch("agents.pr_manager.agent.requests.post")
    @patch("agents.pr_manager.agent.subprocess.run")
    def test_approved_flow(self, mock_run, mock_post, agent):
        # Mock git_clone
        agent.git_clone = MagicMock(return_value="/tmp/devteam/task-1")

        # Mock _get_branch_diff
        agent._get_branch_diff = MagicMock(return_value="some diff")

        # Mock LLM returns approved review
        agent.llm.chat.return_value = json.dumps({
            "approved": True,
            "summary": "LGTM",
            "comments": []
        })

        # Mock PR creation
        pr_response = MagicMock()
        pr_response.status_code = 201
        pr_response.json.return_value = {"html_url": "https://github.com/owner/repo/pull/42"}
        mock_post.return_value = pr_response

        # Mock update_status
        agent.update_status = MagicMock()

        task = {
            "id": "task-1",
            "input": {
                "repo_url": "https://github.com/owner/repo.git",
                "branch": "feature-x",
                "content": "Add new feature"
            }
        }

        agent.handle_task(task)

        agent.git_clone.assert_called_once_with("task-1", "https://github.com/owner/repo.git")
        agent._get_branch_diff.assert_called_once()
        agent.llm.chat.assert_called_once()
        # Should have called update_status with approved
        agent.update_status.assert_called_once()
        status_call = agent.update_status.call_args
        assert "approved" in status_call[1].get("message", "") or "approved" in status_call[0][-1]


class TestHandleTaskChangesRequested:
    @patch("agents.pr_manager.agent.requests.post")
    @patch("agents.pr_manager.agent.subprocess.run")
    def test_changes_requested_flow(self, mock_run, mock_post, agent):
        agent.git_clone = MagicMock(return_value="/tmp/devteam/task-2")
        agent._get_branch_diff = MagicMock(return_value="bad diff")

        agent.llm.chat.return_value = json.dumps({
            "approved": False,
            "summary": "Needs work",
            "comments": [{"file": "main.py", "line": 10, "body": "Fix this"}]
        })

        # Mock PR creation
        pr_response = MagicMock()
        pr_response.status_code = 201
        pr_response.json.return_value = {"html_url": "https://github.com/owner/repo/pull/43"}
        # Label/comment posts
        label_response = MagicMock()
        label_response.status_code = 200
        mock_post.return_value = pr_response

        agent.update_status = MagicMock()

        task = {
            "id": "task-2",
            "input": {
                "repo_url": "https://github.com/owner/repo.git",
                "branch": "feature-y",
                "content": "Fix bug"
            }
        }

        agent.handle_task(task)

        agent.update_status.assert_called_once()
        status_call = agent.update_status.call_args
        assert "changes_requested" in status_call[1].get("message", "") or "changes_requested" in status_call[0][-1]

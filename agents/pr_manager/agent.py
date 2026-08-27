"""PR Manager agent for code review and GitHub PR lifecycle management."""

from __future__ import annotations

import random
import time
from agents.base.agent import BaseAgent
import json
import os
import requests
import subprocess


class PRManagerAgent(BaseAgent):
    def __init__(self):
        super().__init__()
        self.github_token = os.environ.get("GITHUB_TOKEN", "")
        self._default_branch_cache: dict[str, str] = {}

    def _retry_llm(self, messages: list[dict], max_retries: int = 1) -> str:
        """Call LLM with one retry on JSON parse failure."""
        for attempt in range(max_retries + 1):
            response = self.llm.chat(messages)
            try:
                json.loads(response)
                return response
            except json.JSONDecodeError:
                if attempt < max_retries:
                    self.log.warning("LLM response not valid JSON, retrying (attempt %d)", attempt + 1)
                    continue
                # Final attempt — extract JSON from response
                start = response.find("{")
                end = response.rfind("}") + 1
                if start >= 0 and end > start:
                    return response[start:end]
                return response
        return response

    def _get_default_branch(self, repo_url: str) -> str:
        """Detect default branch via GitHub API, with cache."""
        if repo_url in self._default_branch_cache:
            return self._default_branch_cache[repo_url]

        owner_repo = self._extract_owner_repo(repo_url)
        if not owner_repo or not self.github_token:
            return "main"

        for branch in ["main", "master"]:
            try:
                resp = requests.get(
                    f"https://api.github.com/repos/{owner_repo}",
                    headers={"Authorization": f"token {self.github_token}", "Accept": "application/vnd.github.v3+json"},
                    timeout=10,
                )
                if resp.status_code == 200:
                    default = resp.json().get("default_branch", branch)
                    self._default_branch_cache[repo_url] = default
                    return default
            except Exception:
                pass
        return "main"

    def _review_code_with_retry(self, diff: str, task_description: str) -> dict:
        """Use LLM to review code with one retry on parse failure."""
        messages = [
            {"role": "system", "content": (
                "You are a senior code reviewer. Review the diff for: "
                "logic errors, security issues, style problems, correctness. "
                "Respond with JSON: {\"approved\": bool, \"summary\": \"...\", "
                "\"comments\": [{\"file\": \"...\", \"line\": N, \"body\": \"...\"}]}"
            )},
            {"role": "user", "content": f"Task: {task_description}\n\nDiff:\n{diff}"}
        ]
        response = self._retry_llm(messages, max_retries=1)
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            return {"approved": False, "summary": "Failed to parse review", "comments": []}

    def _github_request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Make GitHub API request with exponential backoff for transient errors."""
        max_retries = 3
        headers = kwargs.pop("headers", {})
        headers["Accept"] = "application/vnd.github.v3+json"
        if self.github_token:
            headers["Authorization"] = f"token {self.github_token}"

        for attempt in range(max_retries):
            try:
                resp = requests.request(method, url, headers=headers, timeout=15, **kwargs)
                if resp.status_code in (403, 429, 500, 502, 503):
                    wait = min(30, (2 ** attempt) * random.uniform(1.0, 1.5))
                    self.log.warning("GitHub API %s %s returned %d, retrying in %.1fs", method, url, resp.status_code, wait)
                    time.sleep(wait)
                    continue
                return resp
            except (requests.ConnectionError, requests.Timeout) as e:
                wait = min(30, (2 ** attempt) * random.uniform(1.0, 1.5))
                self.log.warning("GitHub API request failed: %s, retrying in %.1fs", e, wait)
                time.sleep(wait)
        # Final attempt
        return requests.request(method, url, headers=headers, timeout=15, **kwargs)

    def handle_task(self, task: dict) -> None:
        """Review code and manage PR lifecycle."""
        task_id = task["id"]
        repo_url = self.input_value(task, "repo_url")
        branch = self.input_value(task, "branch")
        content = self.input_value(task, "content")
        self.log.info(f"handling task {task_id}: repo={repo_url} branch={branch}")

        # 1. Clone and get the diff
        workspace = self.git_clone(task_id, repo_url)
        default_branch = self._get_default_branch(repo_url)
        diff = self._get_branch_diff(workspace, branch, default_branch)
        self.log.info(f"got diff: {len(diff)} chars")

        # 2. Use LLM to review the code
        review = self._review_code_with_retry(diff, content)
        self.log.info(f"review result: approved={review.get('approved', False)}")

        # 3. Create or update PR on GitHub
        pr_url = self._create_pr(repo_url, branch, default_branch, content, review)
        if pr_url:
            self.log.info(f"PR created: {pr_url}")

        # 4. Based on review, either approve or request changes
        if review.get("approved", False):
            self._add_label(pr_url, "needs-human-review")
            self.update_status(task_id, "completed",
                               message=f"pr:{pr_url}|approved")
        else:
            self._post_review_comments(pr_url, review.get("comments", []))
            self.update_status(task_id, "completed",
                               message=f"pr:{pr_url}|changes_requested")

    def _get_branch_diff(self, workspace: str, branch: str, default_branch: str) -> str:
        """Get the diff between branch and default branch."""
        try:
            subprocess.run(["git", "fetch", "origin", branch], cwd=workspace, capture_output=True, check=True)
            result = subprocess.run(
                ["git", "diff", f"origin/{default_branch}...origin/{branch}"],
                cwd=workspace, capture_output=True, text=True
            )
            if result.stdout:
                return result.stdout
            # Fallback: try main then master
            for fallback in ["main", "master"]:
                if fallback != default_branch:
                    result = subprocess.run(
                        ["git", "diff", f"origin/{fallback}...origin/{branch}"],
                        cwd=workspace, capture_output=True, text=True
                    )
                    if result.stdout:
                        return result.stdout
            self.log.warning("branch diff is empty")
            return ""
        except subprocess.CalledProcessError:
            self.log.warning("failed to get branch diff")
            return ""

    def _review_code(self, diff: str, task_description: str) -> dict:
        """Use LLM to review code changes. Deprecated: use _review_code_with_retry."""
        return self._review_code_with_retry(diff, task_description)

    def _create_pr(self, repo_url: str, branch: str, default_branch: str, title: str, review: dict) -> str:
        """Create a GitHub PR. Returns the PR URL."""
        self.log.info("creating PR on GitHub")
        owner_repo = self._extract_owner_repo(repo_url)
        if not owner_repo or not self.github_token:
            self.log.warning("cannot create PR: missing owner_repo or token")
            return ""

        resp = self._github_request(
            "POST",
            f"https://api.github.com/repos/{owner_repo}/pulls",
            json={
                "title": title[:72],
                "head": branch,
                "base": default_branch,
                "body": review.get("summary", ""),
            },
        )
        if resp.status_code == 201:
            self.log.info("PR created successfully")
            return resp.json().get("html_url", "")
        self.log.warning(f"PR creation failed: status {resp.status_code}")
        return ""

    def _add_label(self, pr_url: str, label: str):
        """Add a label to a GitHub PR."""
        if not pr_url or not self.github_token:
            return
        parts = pr_url.replace("https://github.com/", "").split("/")
        if len(parts) >= 4:
            owner, repo, _, number = parts[0], parts[1], parts[2], parts[3]
            self._github_request(
                "POST",
                f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/labels",
                json={"labels": [label]},
            )

    def _post_review_comments(self, pr_url: str, comments: list):
        """Post review comments on a GitHub PR."""
        self.log.info(f"posting {len(comments)} review comments")
        if not pr_url or not self.github_token:
            return
        parts = pr_url.replace("https://github.com/", "").split("/")
        if len(parts) >= 4:
            owner, repo, _, number = parts[0], parts[1], parts[2], parts[3]
            for comment in comments:
                self._github_request(
                    "POST",
                    f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments",
                    json={"body": f"**{comment.get('file', '')}:{comment.get('line', '')}** - {comment.get('body', '')}"},
                )

    def _extract_owner_repo(self, repo_url: str) -> str:
        """Extract owner/repo from git URL."""
        url = repo_url.replace(".git", "")
        if "github.com" in url:
            parts = url.split("github.com")[-1].strip("/: ")
            return parts
        return ""


if __name__ == "__main__":
    agent = PRManagerAgent()
    agent.run()

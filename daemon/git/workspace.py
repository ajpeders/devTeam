"""Git workspace management — clone, branch, commit, push."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class WorkspaceManager:
    """Manages per-task git workspaces."""

    def __init__(self, workspace_dir: str, default_remote: str = ""):
        self.workspace_dir = Path(workspace_dir)
        self.default_remote = default_remote
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

    def create_workspace(self, task_id: str, repo_url: str = "") -> str:
        url = repo_url or self.default_remote
        if not url:
            raise ValueError("no repo URL provided and no default remote configured")

        ws_path = self.workspace_dir / task_id
        if ws_path.exists():
            return str(ws_path)

        subprocess.run(
            ["git", "clone", url, str(ws_path)],
            check=True,
            capture_output=True,
        )
        return str(ws_path)

    def create_branch(self, workspace_path: str, branch_name: str):
        subprocess.run(
            ["git", "checkout", "-b", branch_name],
            cwd=workspace_path,
            check=True,
            capture_output=True,
        )

    def commit_all(self, workspace_path: str, message: str):
        subprocess.run(
            ["git", "add", "-A"],
            cwd=workspace_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=workspace_path,
            check=True,
            capture_output=True,
        )

    def push(self, workspace_path: str, branch_name: str):
        subprocess.run(
            ["git", "push", "origin", branch_name],
            cwd=workspace_path,
            check=True,
            capture_output=True,
        )

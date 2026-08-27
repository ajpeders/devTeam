"""Deploy agent: Docker build + deploy pipeline."""

from __future__ import annotations

import json
import os
import subprocess

from agents.base.agent import BaseAgent


class DeployAgent(BaseAgent):
    """Builds Docker images and deploys via docker-compose."""

    def __init__(self):
        super().__init__()
        self.registry = os.environ.get("DEVTEAM_DOCKER_REGISTRY", "")
        self.deploy_method = os.environ.get("DEVTEAM_DEPLOY_METHOD", "docker-compose")
        self.deploy_target = os.environ.get("DEVTEAM_DEPLOY_TARGET", "")  # JSON

    def handle_task(self, task: dict) -> None:
        """Merge PR, build Docker image, push, deploy."""
        task_id = task["id"]
        repo_url = self.input_value(task, "repo_url")
        branch = self.input_value(task, "branch")
        pr_url = self.input_value(task, "pr_url")

        self.log.info("handling deploy task repo_url=%s branch=%s", repo_url, branch)

        # 1. Clone repo at the merged branch
        workspace = self.git_clone(task_id, repo_url)
        subprocess.run(
            ["git", "checkout", branch],
            cwd=workspace,
            capture_output=True,
            check=True,
        )
        self.log.info("cloned repo and checked out branch=%s", branch)

        # 2. Build Docker image
        image_tag = self._build_image(workspace, task_id)
        self.log.info("image built: %s", image_tag)

        # 3. Push to registry
        self._push_image(image_tag)
        self.log.info("push step completed")

        # 4. Deploy to target
        target = self._get_deploy_target()
        self._deploy(image_tag, target)
        self.log.info("deploy completed target=%s", target.get("name", "unknown"))

        self.update_status(
            task_id,
            "completed",
            message=f"deployed:{image_tag}|target:{target.get('name', 'unknown')}",
        )

    def _build_image(self, workspace: str, task_id: str) -> str:
        """Build Docker image from workspace. Returns image tag."""
        image_name = os.path.basename(workspace.rstrip("/"))
        tag = (
            f"{self.registry}/{image_name}:{task_id[:8]}"
            if self.registry
            else f"{image_name}:{task_id[:8]}"
        )

        self.log.info("building docker image tag=%s", tag)
        result = subprocess.run(
            ["docker", "build", "-t", tag, "."],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            self.log.warning("docker build failed: %s", result.stderr)
            raise RuntimeError(f"Docker build failed: {result.stderr}")
        self.log.info("docker build succeeded tag=%s", tag)
        return tag

    def _push_image(self, image_tag: str):
        """Push Docker image to registry."""
        if not self.registry:
            self.log.info("no registry configured, skipping push")
            return

        self.log.info("pushing image tag=%s", image_tag)
        result = subprocess.run(
            ["docker", "push", image_tag],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            self.log.warning("docker push failed: %s", result.stderr)
            raise RuntimeError(f"Docker push failed: {result.stderr}")
        self.log.info("push succeeded tag=%s", image_tag)

    def _get_deploy_target(self) -> dict:
        """Get the deploy target configuration."""
        if self.deploy_target:
            try:
                return json.loads(self.deploy_target)
            except json.JSONDecodeError:
                pass
        return {
            "name": "local",
            "host": "localhost",
            "compose_file": "docker-compose.yml",
        }

    def _deploy(self, image_tag: str, target: dict):
        """Deploy the image to the target server."""
        host = target.get("host", "localhost")
        compose_file = target.get("compose_file", "docker-compose.yml")
        method = self.deploy_method

        self.log.info("deploying method=%s host=%s compose_file=%s", method, host, compose_file)

        if method == "docker-compose":
            if host.startswith("ssh://"):
                # Remote deploy via SSH + docker-compose
                env = os.environ.copy()
                env["DOCKER_HOST"] = host
                result = subprocess.run(
                    ["docker", "compose", "-f", compose_file, "up", "-d"],
                    capture_output=True,
                    text=True,
                    env=env,
                )
            else:
                # Local deploy
                result = subprocess.run(
                    ["docker", "compose", "-f", compose_file, "up", "-d"],
                    capture_output=True,
                    text=True,
                )

            if result.returncode != 0:
                self.log.warning("deploy failed: %s", result.stderr)
                raise RuntimeError(f"Deploy failed: {result.stderr}")
            self.log.info("deploy succeeded host=%s", host)


if __name__ == "__main__":
    agent = DeployAgent()
    agent.run()

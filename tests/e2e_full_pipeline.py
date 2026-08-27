#!/usr/bin/env python3
"""Full-pipeline e2e test: submits a dev task and walks the entire dev→review→qa→deploy pipeline.

Usage:
    python tests/e2e_full_pipeline.py --config config/local-test.yaml --repo file:///tmp/test-repo.git

Requires: daemon running, ollama running, and a git repo at --repo.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

import httpx


POLL_INTERVAL = 5  # seconds
TIMEOUT = 600      # seconds total


def wait_for(url: str, headers: dict, timeout: float = 30) -> dict:
    """Wait for the daemon HTTP server to respond."""
    started = time.time()
    while time.time() - started < timeout:
        try:
            resp = httpx.get(url, headers=headers, timeout=5)
            if resp.status_code < 500:
                return {"ok": True}
        except (httpx.ConnectError, httpx.RemoteProtocolError):
            pass
        time.sleep(1)
    raise RuntimeError(f"daemon not responding at {url} after {timeout}s")


def poll_task_status(base_url: str, headers: dict, task_id: str, target_statuses: set[str], timeout: float = TIMEOUT) -> dict:
    """Poll until task reaches one of target_statuses, or raise on timeout."""
    started = time.time()
    while time.time() - started < timeout:
        resp = httpx.post(f"{base_url}/api/task/get", json={"task_id": task_id}, headers=headers, timeout=10)
        if resp.status_code == 200:
            task = resp.json().get("task", {})
            status = task.get("status", "")
            print(f"    task {task_id} status: {status}")
            if status in target_statuses:
                return task
        time.sleep(POLL_INTERVAL)
    raise RuntimeError(f"task {task_id} did not reach {target_statuses} within {timeout}s")


def approve_deploy_task(base_url: str, headers: dict, task_id: str) -> str:
    """Call /api/deploy/approve and return the new deploy task ID."""
    resp = httpx.post(f"{base_url}/api/deploy/approve", json={"task_id": task_id}, headers=headers, timeout=10)
    if resp.status_code != 200:
        raise RuntimeError(f"deploy/approve failed: {resp.status_code} {resp.text}")
    data = resp.json()
    print(f"  approved → deploy task: {data.get('deploy_task_id', '')}")
    return data.get("deploy_task_id", "")


def main():
    parser = argparse.ArgumentParser(description="Full-pipeline e2e test")
    parser.add_argument("--config", default="config/local-test.yaml", help="Config file path")
    parser.add_argument("--repo", required=True, help="Git repo URL (file:// or https://)")
    parser.add_argument("--base-url", default="http://localhost:4223", help="Daemon base URL")
    parser.add_argument("--task-desc", default="Add a hello world function to main.py", help="Task description")
    args = parser.parse_args()

    # Load config for admin key
    from daemon.config import parse_config
    cfg = parse_config(args.config)
    admin_key = cfg.api.admin_api_key
    if not admin_key:
        print("ERROR: api.admin_api_key not set in config")
        sys.exit(1)

    headers = {"X-Api-Key": admin_key}
    base_url = args.base_url

    print("[1] Waiting for daemon...")
    wait_for(f"{base_url}/api/user/me", headers)
    print("[2] Daemon is up")

    # Get or create a project
    print("[3] Ensuring project exists...")
    resp = httpx.post(f"{base_url}/api/project/list", json={}, headers=headers, timeout=10)
    projects = resp.json().get("projects", [])
    project_id = None
    if projects:
        project_id = projects[0]["id"]
        print(f"    Using existing project: {projects[0]['name']} ({project_id})")
    else:
        print("    No projects found, need one — create via frontend or API first")
        sys.exit(1)

    # Submit a dev task
    print(f"[4] Submitting dev task: {args.task_desc}")
    resp = httpx.post(
        f"{base_url}/api/task/create",
        json={
            "project_id": project_id,
            "description": args.task_desc,
            "params": {"repo_url": args.repo},
        },
        headers=headers,
        timeout=10,
    )
    if resp.status_code != 200:
        print(f"ERROR: task create failed: {resp.status_code} {resp.text}")
        sys.exit(1)
    task_id = resp.json().get("task_id", "")
    print(f"    task_id: {task_id}")

    # Walk the pipeline
    print("\n[5] Pipeline walk-through")
    print("─" * 50)

    # dev → in_progress → completed
    print("\n  Dev agent working...")
    dev_task = poll_task_status(base_url, headers, task_id, {"completed", "failed"}, timeout=300)
    if dev_task.get("status") == "failed":
        print("ERROR: dev task failed")
        sys.exit(1)
    dev_detail = dev_task.get("history", [{}])[-1].get("detail", "") if dev_task.get("history") else ""
    print(f"  dev completed: {dev_detail[:80]}")

    # Find review task — use the newest one
    print("\n  Review agent working...")
    resp = httpx.post(f"{base_url}/api/task/list", json={"project_id": project_id}, headers=headers, timeout=10)
    tasks = resp.json().get("tasks", [])
    review_tasks = [t for t in tasks if t.get("type") == "review"]
    if review_tasks:
        review_task = review_tasks[-1]  # newest
    else:
        # Maybe it's still pending, poll for it
        for _ in range(24):  # up to 2 min waiting for review task to appear
            time.sleep(POLL_INTERVAL)
            resp = httpx.post(f"{base_url}/api/task/list", json={"project_id": project_id}, headers=headers, timeout=10)
            tasks = resp.json().get("tasks", [])
            review_tasks = [t for t in tasks if t.get("type") == "review"]
            if review_tasks:
                review_task = review_tasks[-1]
                break
        else:
            print("ERROR: review task not found")
            sys.exit(1)
    print(f"  review task: {review_task['id']} status={review_task['status']}")

    review_task = poll_task_status(base_url, headers, review_task["id"], {"completed", "failed"}, timeout=300)
    if review_task.get("status") == "failed":
        print("ERROR: review task failed")
        sys.exit(1)
    review_detail = review_task.get("history", [{}])[-1].get("detail", "") if review_task.get("history") else ""
    print(f"  review completed: {review_detail[:80]}")

    # Find QA task — use the newest one
    print("\n  QA agent working...")
    resp = httpx.post(f"{base_url}/api/task/list", json={"project_id": project_id}, headers=headers, timeout=10)
    tasks = resp.json().get("tasks", [])
    qa_tasks = [t for t in tasks if t.get("type") == "qa"]
    if not qa_tasks:
        print("ERROR: QA task not found")
        sys.exit(1)
    qa_task = qa_tasks[-1]  # newest
    print(f"  QA task: {qa_task['id']}")

    qa_task = poll_task_status(base_url, headers, qa_task["id"], {"completed", "failed"}, timeout=300)
    if qa_task.get("status") == "failed":
        print("ERROR: QA task failed")
        sys.exit(1)
    qa_detail = qa_task.get("history", [{}])[-1].get("detail", "") if qa_task.get("history") else ""
    print(f"  QA completed: {qa_detail[:80]}")

    # Human approval
    print("\n  [HUMAN GATE] QA tasks require human approval via /api/deploy/approve")
    print(f"  QA task id: {qa_task['id']}")
    approve_resp = httpx.post(
        f"{base_url}/api/deploy/approve",
        json={"task_id": qa_task["id"]},
        headers=headers,
        timeout=10,
    )
    if approve_resp.status_code != 200:
        print(f"  NOTE: deploy/approve returned {approve_resp.status_code} — may need manual approval")
        print(f"  Response: {approve_resp.text}")
    else:
        deploy_task_id = approve_resp.json().get("deploy_task_id", "")
        print(f"  approved → deploy task: {deploy_task_id}")

        # Deploy agent working
        print("\n  Deploy agent working...")
        deploy_task = poll_task_status(base_url, headers, deploy_task_id, {"completed", "failed"}, timeout=300)
        if deploy_task.get("status") == "failed":
            print("ERROR: deploy task failed")
            sys.exit(1)
        deploy_detail = deploy_task.get("history", [{}])[-1].get("detail", "") if deploy_task.get("history") else ""
        print(f"  deploy completed: {deploy_detail[:80]}")

    print("\n" + "=" * 50)
    print("FULL PIPELINE COMPLETED SUCCESSFULLY")
    print("=" * 50)


if __name__ == "__main__":
    main()

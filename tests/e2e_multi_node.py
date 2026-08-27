"""End-to-end multi-node NATS sync test.

Spins up two daemon nodes (node-a, node-b) on different ports sharing the same
NATS server, then verifies:
  1. Task created on node-a syncs to node-b via NATS
  2. Task claimed on node-b updates node-a's local store
  3. Task status change on node-a syncs to node-b
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import requests

DAEMON_A_PORT = 15223
DAEMON_B_PORT = 15224
NATS_URL = "nats://localhost:4222"
ADMIN_KEY_A = "key-node-a-admin"
ADMIN_KEY_B = "key-node-b-admin"
API_BASE_A = f"http://localhost:{DAEMON_A_PORT}"
API_BASE_B = f"http://localhost:{DAEMON_B_PORT}"


def wait_for_api(base_url: str, timeout: int = 15) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{base_url}/docs", timeout=2)
            if r.status_code in (200, 404):
                return True
        except requests.ConnectionError:
            pass
        time.sleep(0.5)
    return False


def api_post(base_url: str, path: str, json: dict, api_key: str) -> requests.Response:
    return requests.post(
        f"{base_url}{path}",
        json=json,
        headers={"X-Api-Key": api_key},
        timeout=10,
    )


def main():
    # ── Temp workspace dirs ────────────────────────────────────
    tmp = tempfile.mkdtemp(prefix="devteam-multi-")
    workspace_a = os.path.join(tmp, "node-a")
    workspace_b = os.path.join(tmp, "node-b")
    os.makedirs(workspace_a, exist_ok=True)
    os.makedirs(workspace_b, exist_ok=True)

    # Copy config template, modifying for each node
    config_a = os.path.join(tmp, "config-a.yaml")
    config_b = os.path.join(tmp, "config-b.yaml")

    with open("config/local-test.yaml") as f:
        cfg_text = f.read()

    cfg_a = cfg_text.replace("node_id: \"local-dev\"", "node_id: \"node-a\"")
    cfg_a = cfg_a.replace("address: \"localhost:4223\"", f"address: \"localhost:{DAEMON_A_PORT}\"")
    cfg_a = cfg_a.replace("admin_api_key: \"devteam-local-admin-key\"", f"admin_api_key: \"{ADMIN_KEY_A}\"")
    cfg_a = cfg_a.replace("workspace_dir: \"/tmp/devteam-workspaces\"", f"workspace_dir: \"{workspace_a}\"")
    cfg_a = cfg_a.replace("seeds: []", f"seeds: [\"{NATS_URL}\"]")

    cfg_b = cfg_text.replace("node_id: \"local-dev\"", "node_id: \"node-b\"")
    cfg_b = cfg_b.replace("address: \"localhost:4223\"", f"address: \"localhost:{DAEMON_B_PORT}\"")
    cfg_b = cfg_b.replace("admin_api_key: \"devteam-local-admin-key\"", f"admin_api_key: \"{ADMIN_KEY_B}\"")
    cfg_b = cfg_b.replace("workspace_dir: \"/tmp/devteam-workspaces\"", f"workspace_dir: \"{workspace_b}\"")
    cfg_b = cfg_b.replace("seeds: []", f"seeds: [\"{NATS_URL}\"]")

    with open(config_a, "w") as f:
        f.write(cfg_a)
    with open(config_b, "w") as f:
        f.write(cfg_b)

    # ── Start both daemon processes ─────────────────────────────
    print("Starting node-a...")
    log_a = open(os.path.join(tmp, "node-a.log"), "w")
    proc_a = subprocess.Popen(
        [sys.executable, "-m", "daemon.main", "--config", config_a],
        stdout=log_a,
        stderr=subprocess.STDOUT,
        cwd=os.getcwd(),
    )

    print("Starting node-b...")
    log_b = open(os.path.join(tmp, "node-b.log"), "w")
    proc_b = subprocess.Popen(
        [sys.executable, "-m", "daemon.main", "--config", config_b],
        stdout=log_b,
        stderr=subprocess.STDOUT,
        cwd=os.getcwd(),
    )

    try:
        # Wait for both APIs to come up
        print("Waiting for node-a API...")
        if not wait_for_api(API_BASE_A):
            print("ERROR: node-a API did not start")
            return 1
        print("node-a ready")

        print("Waiting for node-b API...")
        if not wait_for_api(API_BASE_B):
            print("ERROR: node-b API did not start")
            return 1
        print("node-b ready")

        # Give NATS a moment to establish subscriptions
        time.sleep(1.5)

        # ── Step 1: Create a project on node-a ─────────────────
        print("\n--- Step 1: Create project on node-a ---")
        r = api_post(API_BASE_A, "/api/project/create",
                     {"name": "multi-node-test", "repo_url": "file:///tmp/test.git"},
                     ADMIN_KEY_A)
        if r.status_code != 200:
            print(f"ERROR creating project: {r.status_code} {r.text}")
            return 1
        project_a = r.json()
        project_id = project_a["project_id"]
        print(f"Project created: {project_id}")

        # ── Step 2: Create a dev task on node-a ───────────────
        print("\n--- Step 2: Create dev task on node-a ---")
        r = api_post(API_BASE_A, "/api/task/create",
                     {"project_id": project_id, "description": "sync test task", "priority": 5},
                     ADMIN_KEY_A)
        if r.status_code != 200:
            print(f"ERROR creating task: {r.status_code} {r.text}")
            return 1
        task_a = r.json()
        task_id = task_a["task_id"]
        print(f"Task created: {task_id}")

        # Wait for NATS sync
        time.sleep(2)

        # ── Step 3: Verify task synced to node-b ──────────────
        print("\n--- Step 3: Verify task synced to node-b ---")
        r = api_post(API_BASE_B, "/api/task/get",
                     {"task_id": task_id},
                     ADMIN_KEY_B)
        if r.status_code != 200:
            print(f"ERROR: task not found on node-b after sync: {r.status_code} {r.text}")
            return 1
        task_b = r.json()["task"]
        assert task_b["status"] == "pending", f"Expected pending, got {task_b['status']}"
        assert task_b["priority"] == 5, f"Expected priority 5, got {task_b['priority']}"
        print(f"Task on node-b: status={task_b['status']}, priority={task_b['priority']} ✓")

        # ── Step 4: Claim task on node-b (via /api/task/claim) ──
        print("\n--- Step 4: Claim task on node-b ---")
        r = requests.post(
            f"{API_BASE_B}/api/task/claim",
            json={"agent_id": "agent-b-0", "agent_type": "dev", "node_id": "node-b"},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"ERROR claiming task: {r.status_code} {r.text}")
            return 1
        claim_resp = r.json()
        if not claim_resp["found"]:
            print("ERROR: no pending tasks found to claim on node-b")
            return 1
        claimed_task = claim_resp["task"]
        print(f"Claimed task: {claimed_task['id']} on node-b ✓")

        # Wait for NATS claim event to propagate
        time.sleep(2)

        # ── Step 5: Verify task is claimed on node-a ────────────
        print("\n--- Step 5: Verify claim synced back to node-a ---")
        r = api_post(API_BASE_A, "/api/task/get",
                     {"task_id": task_id},
                     ADMIN_KEY_A)
        if r.status_code != 200:
            print(f"ERROR getting task from node-a: {r.status_code} {r.text}")
            return 1
        task_a_updated = r.json()["task"]
        assert task_a_updated["status"] == "assigned", f"Expected assigned, got {task_a_updated['status']}"
        assert task_a_updated["assigned_to"] == "agent-b-0", f"Expected agent-b-0, got {task_a_updated['assigned_to']}"
        print(f"Task on node-a after claim: status={task_a_updated['status']}, "
              f"assigned_to={task_a_updated['assigned_to']} ✓")

        # ── Step 6: Update status to in_progress on node-a ─────
        print("\n--- Step 6: Update status to in_progress on node-a ---")
        r = requests.post(
            f"{API_BASE_A}/api/task/status",
            json={"task_id": task_id, "status": "in_progress", "detail": "working on it"},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"ERROR updating status: {r.status_code} {r.text}")
            return 1
        print("Status updated to in_progress ✓")

        time.sleep(2)

        # ── Step 7: Verify status change synced to node-b ───────
        print("\n--- Step 7: Verify status change synced to node-b ---")
        r = api_post(API_BASE_B, "/api/task/get",
                     {"task_id": task_id},
                     ADMIN_KEY_B)
        if r.status_code != 200:
            print(f"ERROR getting task from node-b: {r.status_code} {r.text}")
            return 1
        task_b_updated = r.json()["task"]
        assert task_b_updated["status"] == "in_progress", \
            f"Expected in_progress, got {task_b_updated['status']}"
        print(f"Task on node-b after status change: status={task_b_updated['status']} ✓")

        print("\n✅ All multi-node NATS sync tests passed!")

    finally:
        # Print daemon logs before cleanup
        for node_name, log_fd in [("node-a", log_a), ("node-b", log_b)]:
            log_fd.flush()
            try:
                with open(log_fd.name) as f:
                    content = f.read()
                if content:
                    print(f"\n=== {node_name} logs (last 3000 chars) ===")
                    print(content[-3000:])
            except Exception:
                pass

        # Shutdown both nodes
        print("\nShutting down nodes...")
        proc_a.terminate()
        proc_b.terminate()
        try:
            proc_a.wait(timeout=5)
            proc_b.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc_a.kill()
            proc_b.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())

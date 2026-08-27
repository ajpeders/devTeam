"""Tests for FastAPI server — auth, project access control, and task pipeline."""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest

from daemon.api.server import DaemonServer
from daemon.nats.sync import TaskSyncer
from daemon.tasks.models import User
from daemon.tasks.store import TaskStore

ADMIN_KEY = "test-admin-key-12345"
AGENT_KEY = "test-agent-key-67890"


@pytest.fixture()
def store(tmp_path):
    s = TaskStore(str(tmp_path / "test.db"))
    # Create admin user
    admin = User(name="admin", api_key=ADMIN_KEY, is_admin=True)
    s.create_user(admin)
    yield s
    s.close()


class _Response:
    def __init__(self, status_code: int, text: str, headers: dict[str, str]):
        self.status_code = status_code
        self.text = text
        self.headers = headers

    def json(self):
        return json.loads(self.text)


class _Client:
    def __init__(self, app):
        self.app = app

    def request(self, method: str, url: str, **kwargs) -> _Response:
        async def _run():
            transport = httpx.ASGITransport(app=self.app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                task = asyncio.create_task(client.request(method, url, **kwargs))
                await asyncio.sleep(0.01)
                response = await task
                return _Response(response.status_code, response.text, dict(response.headers))

        return asyncio.run(_run())

    def post(self, url: str, **kwargs) -> _Response:
        return self.request("POST", url, **kwargs)

    def get(self, url: str, **kwargs) -> _Response:
        return self.request("GET", url, **kwargs)

    def patch(self, url: str, **kwargs) -> _Response:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs) -> _Response:
        return self.request("DELETE", url, **kwargs)


@pytest.fixture()
def client(store):
    syncer = TaskSyncer(None, store, "test-node")
    srv = DaemonServer(
        store=store, syncer=syncer, workspace=None, node_id="test-node",
        agent_secret=AGENT_KEY,
    )
    return _Client(srv.app)


@pytest.fixture()
def client_no_agent_secret(store):
    """Client constructed without an agent_secret — used to verify the fail-closed
    behavior of agent-internal endpoints when the daemon has no shared secret."""
    syncer = TaskSyncer(None, store, "test-node")
    srv = DaemonServer(
        store=store, syncer=syncer, workspace=None, node_id="test-node",
        agent_secret="",
    )
    return _Client(srv.app)


def _agent_headers() -> dict:
    return {"X-Agent-Key": AGENT_KEY}


def _admin_headers() -> dict:
    return {"X-Api-Key": ADMIN_KEY}


def _create_user(client, name: str = "alice") -> dict:
    """Create a user via admin endpoint, return {"user_id", "api_key"}."""
    resp = client.post("/api/admin/user/create", headers=_admin_headers(), json={"name": name})
    assert resp.status_code == 200
    data = resp.json()
    assert data["user_id"]
    assert data["api_key"]
    return data


def _headers(api_key: str) -> dict:
    return {"X-Api-Key": api_key}


# ─── Admin user management ────────────────────────────────────


class TestAdminUserManagement:
    def test_admin_creates_user(self, client):
        data = _create_user(client)
        assert len(data["api_key"]) > 10

    def test_non_admin_cannot_create_user(self, client):
        user = _create_user(client, "regular")
        resp = client.post("/api/admin/user/create", headers=_headers(user["api_key"]),
                           json={"name": "hacker"})
        assert resp.status_code == 403

    def test_admin_lists_users(self, client):
        _create_user(client, "bob")
        resp = client.post("/api/admin/user/list", headers=_admin_headers())
        assert resp.status_code == 200
        users = resp.json()["users"]
        names = [u["name"] for u in users]
        assert "admin" in names
        assert "bob" in names

    def test_admin_deletes_user(self, client):
        user = _create_user(client, "deleteme")
        resp = client.post("/api/admin/user/delete", headers=_admin_headers(),
                           json={"user_id": user["user_id"]})
        assert resp.status_code == 200
        # Verify deleted
        resp = client.post("/api/user/me", headers=_headers(user["api_key"]))
        assert resp.status_code == 401

    def test_admin_cannot_delete_self(self, client):
        # Get admin user_id
        resp = client.post("/api/user/me", headers=_admin_headers())
        admin_id = resp.json()["id"]
        resp = client.post("/api/admin/user/delete", headers=_admin_headers(),
                           json={"user_id": admin_id})
        assert resp.status_code == 400

    def test_get_me(self, client):
        reg = _create_user(client)
        resp = client.post("/api/user/me", headers=_headers(reg["api_key"]))
        assert resp.status_code == 200
        assert resp.json()["name"] == "alice"
        assert resp.json()["is_admin"] is False

    def test_admin_get_me_shows_admin(self, client):
        resp = client.post("/api/user/me", headers=_admin_headers())
        assert resp.status_code == 200
        assert resp.json()["is_admin"] is True


# ─── Auth enforcement ─────────────────────────────────────────


class TestAuthEnforcement:
    def test_no_key_returns_422(self, client):
        # FastAPI returns 422 when required header is missing
        resp = client.post("/api/project/list")
        assert resp.status_code == 422

    def test_bad_key_returns_401(self, client):
        resp = client.post("/api/project/list", headers=_headers("bogus-key"))
        assert resp.status_code == 401
        assert "invalid API key" in resp.json()["detail"]

    def test_agent_endpoint_requires_agent_key(self, client):
        # Agent-internal endpoints require a matching X-Agent-Key header.
        # Missing header → 403 (fail-closed).
        resp = client.post("/api/heartbeat", json={"agent_id": "test-agent"})
        assert resp.status_code == 403

    def test_agent_endpoint_rejects_wrong_agent_key(self, client):
        resp = client.post(
            "/api/heartbeat",
            headers={"X-Agent-Key": "wrong"},
            json={"agent_id": "test-agent"},
        )
        assert resp.status_code == 403

    def test_agent_endpoint_accepts_correct_agent_key(self, client):
        resp = client.post(
            "/api/heartbeat",
            headers=_agent_headers(),
            json={"agent_id": "test-agent"},
        )
        assert resp.status_code == 200

    def test_agent_endpoints_fail_closed_when_secret_unset(self, client_no_agent_secret):
        """When the daemon is started without an agent_secret, agent-internal
        endpoints must reject every request, including ones with no header or
        a guessed key. Operating without a shared secret would let any caller
        mutate task state via /api/task/claim, /api/task/status, etc."""
        # No header
        resp = client_no_agent_secret.post("/api/heartbeat", json={"agent_id": "x"})
        assert resp.status_code == 403
        # Empty key (matches the empty configured secret naively — must still reject)
        resp = client_no_agent_secret.post(
            "/api/heartbeat", headers={"X-Agent-Key": ""}, json={"agent_id": "x"},
        )
        assert resp.status_code == 403
        # Any guessed key
        resp = client_no_agent_secret.post(
            "/api/heartbeat", headers={"X-Agent-Key": "guess"}, json={"agent_id": "x"},
        )
        assert resp.status_code == 403


# ─── Additional API keys per user ────────────────────────────


class TestApiKeyManagement:
    def test_create_returns_plaintext_once(self, client):
        reg = _create_user(client)
        resp = client.post("/api/key/create", headers=_headers(reg["api_key"]), json={"label": "ci"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["label"] == "ci"
        assert len(data["key"]) >= 32
        assert data["id"]

    def test_new_key_authenticates(self, client):
        reg = _create_user(client)
        new_key = client.post(
            "/api/key/create", headers=_headers(reg["api_key"]), json={"label": "prod"}
        ).json()["key"]
        resp = client.post("/api/user/me", headers=_headers(new_key))
        assert resp.status_code == 200
        assert resp.json()["name"] == "alice"

    def test_list_returns_only_callers_keys(self, client):
        alice = _create_user(client, "alice")
        bob = _create_user(client, "bob")
        client.post("/api/key/create", headers=_headers(alice["api_key"]), json={"label": "alice-ci"})
        client.post("/api/key/create", headers=_headers(alice["api_key"]), json={"label": "alice-prod"})
        client.post("/api/key/create", headers=_headers(bob["api_key"]), json={"label": "bob-ci"})

        alice_keys = client.post("/api/key/list", headers=_headers(alice["api_key"])).json()["keys"]
        bob_keys = client.post("/api/key/list", headers=_headers(bob["api_key"])).json()["keys"]
        assert sorted(k["label"] for k in alice_keys) == ["alice-ci", "alice-prod"]
        assert [k["label"] for k in bob_keys] == ["bob-ci"]

    def test_list_does_not_leak_plaintext_or_hash(self, client):
        reg = _create_user(client)
        client.post("/api/key/create", headers=_headers(reg["api_key"]), json={"label": "ci"})
        keys = client.post("/api/key/list", headers=_headers(reg["api_key"])).json()["keys"]
        assert keys and "key" not in keys[0] and "key_hash" not in keys[0]

    def test_delete_revokes_auth(self, client):
        reg = _create_user(client)
        new_key = client.post(
            "/api/key/create", headers=_headers(reg["api_key"]), json={"label": "temp"}
        ).json()
        # Pre-delete: key authenticates
        assert client.post("/api/user/me", headers=_headers(new_key["key"])).status_code == 200
        # Delete
        resp = client.post(
            "/api/key/delete", headers=_headers(reg["api_key"]), json={"key_id": new_key["id"]}
        )
        assert resp.status_code == 200
        # Post-delete: key fails auth
        assert client.post("/api/user/me", headers=_headers(new_key["key"])).status_code == 401

    def test_cannot_delete_another_users_key(self, client):
        alice = _create_user(client, "alice")
        bob = _create_user(client, "bob")
        alice_key = client.post(
            "/api/key/create", headers=_headers(alice["api_key"]), json={"label": "alice-only"}
        ).json()
        resp = client.post(
            "/api/key/delete", headers=_headers(bob["api_key"]), json={"key_id": alice_key["id"]}
        )
        assert resp.status_code == 404
        # Alice's key still works
        assert client.post("/api/user/me", headers=_headers(alice_key["key"])).status_code == 200

    def test_delete_invalid_uuid_returns_400(self, client):
        reg = _create_user(client)
        resp = client.post(
            "/api/key/delete", headers=_headers(reg["api_key"]), json={"key_id": "not-a-uuid"}
        )
        assert resp.status_code == 400

    def test_primary_user_key_not_listed(self, client):
        # The per-user primary key on UserRow is not surfaced via /api/key/list —
        # only additive keys appear. This keeps the legacy primary auditable separately.
        reg = _create_user(client)
        keys = client.post("/api/key/list", headers=_headers(reg["api_key"])).json()["keys"]
        assert keys == []


# ─── Platform-admin env override & admin-email allowlist ────


class TestPlatformAdminEnvOverride:
    """Tests for MYDEVTEAM_API_KEY (synthetic admin override) and
    MYDEVTEAM_ADMIN_EMAILS (auto-admin elevation by email). Both must
    fail-closed when unset: no env var = no override, no auto-admin."""

    def test_env_unset_admin_endpoints_reject_unknown_key(self, client, monkeypatch):
        monkeypatch.delenv("MYDEVTEAM_API_KEY", raising=False)
        # A key not in the DB should not authenticate even if env override is empty.
        resp = client.post(
            "/api/admin/user/list", headers=_headers("hopeful-platform-admin-key"),
        )
        assert resp.status_code == 401

    def test_env_unset_random_request_to_admin_endpoint_rejected(self, client, monkeypatch):
        # Specifically the fail-closed assertion: with MYDEVTEAM_API_KEY unset,
        # an arbitrary X-Api-Key value must not slip into admin endpoints.
        monkeypatch.delenv("MYDEVTEAM_API_KEY", raising=False)
        resp = client.post("/api/admin/user/list", headers=_headers(""))
        assert resp.status_code == 401

    def test_env_set_grants_admin_to_matching_key(self, client, monkeypatch):
        monkeypatch.setenv("MYDEVTEAM_API_KEY", "platform-override-key")
        resp = client.post(
            "/api/admin/user/list", headers=_headers("platform-override-key"),
        )
        assert resp.status_code == 200
        # And /api/user/me identifies the caller as admin.
        me = client.post(
            "/api/user/me", headers=_headers("platform-override-key"),
        ).json()
        assert me["is_admin"] is True
        assert me["name"] == "platform-admin"

    def test_env_set_but_wrong_key_still_rejected(self, client, monkeypatch):
        monkeypatch.setenv("MYDEVTEAM_API_KEY", "platform-override-key")
        resp = client.post(
            "/api/admin/user/list", headers=_headers("not-the-override"),
        )
        assert resp.status_code == 401

    def test_admin_emails_unset_no_auto_admin(self, client, monkeypatch):
        monkeypatch.delenv("MYDEVTEAM_ADMIN_EMAILS", raising=False)
        # Create a user with an email — without env allowlist they remain non-admin
        # even if their email looks important.
        resp = client.post(
            "/api/admin/user/create",
            headers=_admin_headers(),
            json={"name": "alex", "email": "alex@example.com"},
        )
        assert resp.status_code == 200
        api_key = resp.json()["api_key"]
        resp = client.post("/api/admin/user/list", headers=_headers(api_key))
        assert resp.status_code == 403

    def test_admin_emails_set_promotes_matching_email(self, client, monkeypatch):
        monkeypatch.setenv("MYDEVTEAM_ADMIN_EMAILS", "ops@example.com,alex@example.com")
        resp = client.post(
            "/api/admin/user/create",
            headers=_admin_headers(),
            json={"name": "alex", "email": "alex@example.com"},
        )
        api_key = resp.json()["api_key"]
        # Auto-elevated → admin endpoints accept this user.
        resp = client.post("/api/admin/user/list", headers=_headers(api_key))
        assert resp.status_code == 200

    def test_admin_emails_case_insensitive(self, client, monkeypatch):
        monkeypatch.setenv("MYDEVTEAM_ADMIN_EMAILS", "OPS@example.com")
        resp = client.post(
            "/api/admin/user/create",
            headers=_admin_headers(),
            json={"name": "ops", "email": "ops@EXAMPLE.com"},
        )
        api_key = resp.json()["api_key"]
        resp = client.post("/api/admin/user/list", headers=_headers(api_key))
        assert resp.status_code == 200

    def test_admin_emails_non_match_stays_non_admin(self, client, monkeypatch):
        monkeypatch.setenv("MYDEVTEAM_ADMIN_EMAILS", "ops@example.com")
        resp = client.post(
            "/api/admin/user/create",
            headers=_admin_headers(),
            json={"name": "alice", "email": "alice@example.com"},
        )
        api_key = resp.json()["api_key"]
        resp = client.post("/api/admin/user/list", headers=_headers(api_key))
        assert resp.status_code == 403


# ─── Project access control ──────────────────────────────────


class TestProjectAccess:
    def test_create_and_list_project(self, client):
        reg = _create_user(client)
        h = _headers(reg["api_key"])

        resp = client.post("/api/project/create", headers=h, json={
            "name": "my-app", "repo_url": "file:///tmp/repo",
        })
        assert resp.status_code == 200
        project_id = resp.json()["project_id"]

        resp = client.post("/api/project/list", headers=h)
        assert resp.status_code == 200
        projects = resp.json()["projects"]
        assert len(projects) == 1
        assert projects[0]["name"] == "my-app"
        assert projects[0]["id"] == project_id

    def test_user_isolation(self, client):
        """User A cannot see User B's projects."""
        # Register two users
        alice = _create_user(client, "alice")
        bob = _create_user(client, "bob")

        # Alice creates a project
        client.post("/api/project/create", headers=_headers(alice["api_key"]), json={
            "name": "alice-app", "repo_url": "file:///tmp/alice",
        })

        # Bob creates a project
        client.post("/api/project/create", headers=_headers(bob["api_key"]), json={
            "name": "bob-app", "repo_url": "file:///tmp/bob",
        })

        # Alice only sees her project
        alice_projects = client.post("/api/project/list", headers=_headers(alice["api_key"])).json()["projects"]
        assert len(alice_projects) == 1
        assert alice_projects[0]["name"] == "alice-app"

        # Bob only sees his project
        bob_projects = client.post("/api/project/list", headers=_headers(bob["api_key"])).json()["projects"]
        assert len(bob_projects) == 1
        assert bob_projects[0]["name"] == "bob-app"

    def test_cross_user_project_access_denied(self, client):
        """User A cannot delete User B's project."""
        alice = _create_user(client, "alice")
        bob = _create_user(client, "bob")

        # Alice creates a project
        resp = client.post("/api/project/create", headers=_headers(alice["api_key"]), json={
            "name": "alice-app", "repo_url": "file:///tmp/alice",
        })
        alice_project_id = resp.json()["project_id"]

        # Bob tries to delete Alice's project → 404
        resp = client.post("/api/project/delete", headers=_headers(bob["api_key"]), json={
            "project_id": alice_project_id,
        })
        assert resp.status_code == 404

    def test_delete_project(self, client):
        reg = _create_user(client)
        h = _headers(reg["api_key"])

        resp = client.post("/api/project/create", headers=h, json={
            "name": "to-delete", "repo_url": "file:///tmp/repo",
        })
        project_id = resp.json()["project_id"]

        resp = client.post("/api/project/delete", headers=h, json={"project_id": project_id})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Project no longer listed
        projects = client.post("/api/project/list", headers=h).json()["projects"]
        assert len(projects) == 0


# ─── Task CRUD with auth ─────────────────────────────────────


class TestTaskCRUD:
    def _setup_project(self, client) -> tuple[str, dict]:
        """Register user + create project, return (project_id, headers)."""
        reg = _create_user(client)
        h = _headers(reg["api_key"])
        resp = client.post("/api/project/create", headers=h, json={
            "name": "test-proj", "repo_url": "file:///tmp/repo",
        })
        return resp.json()["project_id"], h

    def test_create_and_get_task(self, client):
        project_id, h = self._setup_project(client)

        resp = client.post("/api/task/create", headers=h, json={
            "project_id": project_id, "description": "Build feature X",
        })
        assert resp.status_code == 200
        task_id = resp.json()["task_id"]

        resp = client.post("/api/task/get", headers=h, json={"task_id": task_id})
        assert resp.status_code == 200
        task = resp.json()["task"]
        assert task["type"] == "dev"
        assert task["status"] == "pending"
        assert task["input"]["description"] == "Build feature X"
        assert task["project_id"] == project_id

    def test_list_tasks_filtered_by_project(self, client):
        reg = _create_user(client)
        h = _headers(reg["api_key"])

        # Create two projects
        p1 = client.post("/api/project/create", headers=h, json={
            "name": "proj-1", "repo_url": "file:///tmp/r1",
        }).json()["project_id"]
        p2 = client.post("/api/project/create", headers=h, json={
            "name": "proj-2", "repo_url": "file:///tmp/r2",
        }).json()["project_id"]

        # Create tasks in each
        client.post("/api/task/create", headers=h, json={"project_id": p1, "description": "Task A"})
        client.post("/api/task/create", headers=h, json={"project_id": p2, "description": "Task B"})

        # List all → both tasks
        all_tasks = client.post("/api/task/list", headers=h, json={}).json()["tasks"]
        assert len(all_tasks) == 2

        # List by project → only one
        p1_tasks = client.post("/api/task/list", headers=h, json={"project_id": p1}).json()["tasks"]
        assert len(p1_tasks) == 1
        assert p1_tasks[0]["input"]["description"] == "Task A"

    def test_cross_user_task_access_denied(self, client):
        """User B cannot see User A's tasks."""
        alice = _create_user(client, "alice")
        bob = _create_user(client, "bob")

        # Alice creates project + task
        p = client.post("/api/project/create", headers=_headers(alice["api_key"]), json={
            "name": "alice-proj", "repo_url": "file:///tmp/a",
        }).json()["project_id"]
        t = client.post("/api/task/create", headers=_headers(alice["api_key"]), json={
            "project_id": p, "description": "Secret task",
        }).json()["task_id"]

        # Bob tries to get Alice's task → 404
        resp = client.post("/api/task/get", headers=_headers(bob["api_key"]), json={"task_id": t})
        assert resp.status_code == 404

    def test_cancel_task(self, client):
        project_id, h = self._setup_project(client)

        task_id = client.post("/api/task/create", headers=h, json={
            "project_id": project_id, "description": "Cancel me",
        }).json()["task_id"]

        resp = client.post("/api/task/cancel", headers=h, json={"task_id": task_id})
        assert resp.status_code == 200

        task = client.post("/api/task/get", headers=h, json={"task_id": task_id}).json()["task"]
        assert task["status"] == "cancelled"

    def test_delete_task(self, client):
        project_id, h = self._setup_project(client)

        task_id = client.post("/api/task/create", headers=h, json={
            "project_id": project_id, "description": "Delete me",
        }).json()["task_id"]

        resp = client.post("/api/task/delete", headers=h, json={"task_id": task_id})
        assert resp.status_code == 200

        resp = client.post("/api/task/get", headers=h, json={"task_id": task_id})
        assert resp.status_code == 404


# ─── End-to-end: task pipeline with agent claim ──────────────


class TestTaskPipeline:
    """Full flow: register → project → submit → agent claim → complete → chaining."""

    def test_agent_claims_project_scoped_task(self, client, store):
        """Agent with matching project_id can claim a task."""
        reg = _create_user(client)
        h = _headers(reg["api_key"])

        # Create project + task
        project_id = client.post("/api/project/create", headers=h, json={
            "name": "claim-test", "repo_url": "file:///tmp/repo",
        }).json()["project_id"]

        client.post("/api/task/create", headers=h, json={
            "project_id": project_id, "description": "Hello world app",
        })

        # Agent claims with matching project_id
        resp = client.post("/api/task/claim", headers=_agent_headers(), json={
            "agent_id": "dev-0", "agent_type": "dev", "project_id": project_id,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["found"] is True
        assert data["task"]["type"] == "dev"
        assert data["task"]["project_id"] == project_id

    def test_agent_cannot_claim_wrong_project(self, client):
        """Agent with different project_id cannot claim the task."""
        reg = _create_user(client)
        h = _headers(reg["api_key"])

        project_id = client.post("/api/project/create", headers=h, json={
            "name": "scoped", "repo_url": "file:///tmp/repo",
        }).json()["project_id"]

        client.post("/api/task/create", headers=h, json={
            "project_id": project_id, "description": "Scoped task",
        })

        # Agent claims with WRONG project_id → not found
        wrong_id = str(uuid.uuid4())
        resp = client.post("/api/task/claim", headers=_agent_headers(), json={
            "agent_id": "dev-0", "agent_type": "dev", "project_id": wrong_id,
        })
        assert resp.json()["found"] is False

    def test_double_claim_returns_not_found(self, client):
        """Second agent claiming the same single task gets found=False, not 500."""
        reg = _create_user(client)
        h = _headers(reg["api_key"])

        project_id = client.post("/api/project/create", headers=h, json={
            "name": "race-test", "repo_url": "file:///tmp/repo",
        }).json()["project_id"]

        client.post("/api/task/create", headers=h, json={
            "project_id": project_id, "description": "Only one task",
        })

        # First claim succeeds
        resp1 = client.post("/api/task/claim", headers=_agent_headers(), json={
            "agent_id": "dev-0", "agent_type": "dev", "project_id": project_id,
        })
        assert resp1.status_code == 200
        assert resp1.json()["found"] is True

        # Second claim for same type/project returns not-found (no 500)
        resp2 = client.post("/api/task/claim", headers=_agent_headers(), json={
            "agent_id": "dev-1", "agent_type": "dev", "project_id": project_id,
        })
        assert resp2.status_code == 200
        assert resp2.json()["found"] is False

    def test_completion_chains_dev_to_review(self, client, store):
        """Completing a dev task auto-creates a review task in the same project."""
        reg = _create_user(client)
        h = _headers(reg["api_key"])

        project_id = client.post("/api/project/create", headers=h, json={
            "name": "pipeline", "repo_url": "file:///tmp/repo",
        }).json()["project_id"]

        client.post("/api/task/create", headers=h, json={
            "project_id": project_id, "description": "Build feature",
        })

        # Agent claims + completes dev task
        claim = client.post("/api/task/claim", headers=_agent_headers(), json={
            "agent_id": "dev-0", "agent_type": "dev", "project_id": project_id,
        }).json()
        task_id = claim["task"]["id"]

        client.post("/api/task/status", headers=_agent_headers(), json={
            "task_id": task_id, "status": "in_progress", "detail": "working",
        })
        client.post("/api/task/status", headers=_agent_headers(), json={
            "task_id": task_id, "status": "completed", "detail": "branch:feature-x|done",
        })

        # Review task should be auto-created in the same project
        review_claim = client.post("/api/task/claim", headers=_agent_headers(), json={
            "agent_id": "review-0", "agent_type": "review", "project_id": project_id,
        }).json()
        assert review_claim["found"] is True
        assert review_claim["task"]["type"] == "review"
        assert review_claim["task"]["project_id"] == project_id

    def test_review_approved_chains_to_qa(self, client, store):
        """Review approved → QA task auto-created."""
        reg = _create_user(client)
        h = _headers(reg["api_key"])

        project_id = client.post("/api/project/create", headers=h, json={
            "name": "full-pipe", "repo_url": "file:///tmp/repo",
        }).json()["project_id"]

        client.post("/api/task/create", headers=h, json={
            "project_id": project_id, "description": "Full pipeline test",
        })

        # Dev claims + completes
        dev = client.post("/api/task/claim", headers=_agent_headers(), json={
            "agent_id": "dev-0", "agent_type": "dev", "project_id": project_id,
        }).json()["task"]
        client.post("/api/task/status", headers=_agent_headers(), json={
            "task_id": dev["id"], "status": "in_progress",
        })
        client.post("/api/task/status", headers=_agent_headers(), json={
            "task_id": dev["id"], "status": "completed", "detail": "done",
        })

        # Review claims + approves
        review = client.post("/api/task/claim", headers=_agent_headers(), json={
            "agent_id": "review-0", "agent_type": "review", "project_id": project_id,
        }).json()["task"]
        client.post("/api/task/status", headers=_agent_headers(), json={
            "task_id": review["id"], "status": "in_progress",
        })
        client.post("/api/task/status", headers=_agent_headers(), json={
            "task_id": review["id"], "status": "completed", "detail": "approved",
        })

        # QA task auto-created
        qa = client.post("/api/task/claim", headers=_agent_headers(), json={
            "agent_id": "qa-0", "agent_type": "qa", "project_id": project_id,
        }).json()
        assert qa["found"] is True
        assert qa["task"]["type"] == "qa"

    def test_qa_completed_does_not_auto_deploy(self, client, store):
        """QA completion requires human approval — no auto-deploy."""
        reg = _create_user(client)
        h = _headers(reg["api_key"])

        project_id = client.post("/api/project/create", headers=h, json={
            "name": "gate-test", "repo_url": "file:///tmp/repo",
        }).json()["project_id"]

        client.post("/api/task/create", headers=h, json={
            "project_id": project_id, "description": "Gate test",
        })

        # Dev → review → qa
        dev = client.post("/api/task/claim", headers=_agent_headers(), json={
            "agent_id": "dev-0", "agent_type": "dev", "project_id": project_id,
        }).json()["task"]
        client.post("/api/task/status", headers=_agent_headers(), json={"task_id": dev["id"], "status": "in_progress"})
        client.post("/api/task/status", headers=_agent_headers(), json={"task_id": dev["id"], "status": "completed", "detail": "done"})

        review = client.post("/api/task/claim", headers=_agent_headers(), json={
            "agent_id": "review-0", "agent_type": "review", "project_id": project_id,
        }).json()["task"]
        client.post("/api/task/status", headers=_agent_headers(), json={"task_id": review["id"], "status": "in_progress"})
        client.post("/api/task/status", headers=_agent_headers(), json={"task_id": review["id"], "status": "completed", "detail": "approved"})

        qa = client.post("/api/task/claim", headers=_agent_headers(), json={
            "agent_id": "qa-0", "agent_type": "qa", "project_id": project_id,
        }).json()["task"]
        client.post("/api/task/status", headers=_agent_headers(), json={"task_id": qa["id"], "status": "in_progress"})
        client.post("/api/task/status", headers=_agent_headers(), json={"task_id": qa["id"], "status": "completed", "detail": "all tests pass"})

        # No deploy task auto-created — human gate
        deploy = client.post("/api/task/claim", headers=_agent_headers(), json={
            "agent_id": "deploy-0", "agent_type": "deploy", "project_id": project_id,
        }).json()
        assert deploy["found"] is False

    def test_approve_deploy_via_api(self, client, store):
        """Human approves deploy via API after QA passes."""
        reg = _create_user(client)
        h = _headers(reg["api_key"])

        project_id = client.post("/api/project/create", headers=h, json={
            "name": "deploy-test", "repo_url": "file:///tmp/repo",
        }).json()["project_id"]

        client.post("/api/task/create", headers=h, json={
            "project_id": project_id, "description": "Deploy test",
        })

        # Full pipeline: dev → review → qa
        dev = client.post("/api/task/claim", headers=_agent_headers(), json={
            "agent_id": "dev-0", "agent_type": "dev", "project_id": project_id,
        }).json()["task"]
        client.post("/api/task/status", headers=_agent_headers(), json={"task_id": dev["id"], "status": "in_progress"})
        client.post("/api/task/status", headers=_agent_headers(), json={"task_id": dev["id"], "status": "completed", "detail": "done"})

        review = client.post("/api/task/claim", headers=_agent_headers(), json={
            "agent_id": "review-0", "agent_type": "review", "project_id": project_id,
        }).json()["task"]
        client.post("/api/task/status", headers=_agent_headers(), json={"task_id": review["id"], "status": "in_progress"})
        client.post("/api/task/status", headers=_agent_headers(), json={"task_id": review["id"], "status": "completed", "detail": "approved"})

        qa = client.post("/api/task/claim", headers=_agent_headers(), json={
            "agent_id": "qa-0", "agent_type": "qa", "project_id": project_id,
        }).json()["task"]
        client.post("/api/task/status", headers=_agent_headers(), json={"task_id": qa["id"], "status": "in_progress"})
        client.post("/api/task/status", headers=_agent_headers(), json={"task_id": qa["id"], "status": "completed", "detail": "pass"})

        # Human approves deploy
        resp = client.post("/api/deploy/approve", headers=h, json={"task_id": qa["id"]})
        assert resp.status_code == 200
        deploy_task_id = resp.json()["deploy_task_id"]

        # Deploy agent can now claim it
        deploy = client.post("/api/task/claim", headers=_agent_headers(), json={
            "agent_id": "deploy-0", "agent_type": "deploy", "project_id": project_id,
        }).json()
        assert deploy["found"] is True
        assert deploy["task"]["id"] == deploy_task_id
        assert deploy["task"]["project_id"] == project_id


# ─── Dashboard stats ─────────────────────────────────────────


class TestDashboard:
    def test_stats_scoped_to_user(self, client):
        alice = _create_user(client, "alice")
        bob = _create_user(client, "bob")

        # Alice project + task
        p = client.post("/api/project/create", headers=_headers(alice["api_key"]), json={
            "name": "alice-proj", "repo_url": "file:///tmp/a",
        }).json()["project_id"]
        client.post("/api/task/create", headers=_headers(alice["api_key"]), json={
            "project_id": p, "description": "Alice task",
        })

        # Bob sees zero tasks
        stats = client.post("/api/dashboard/stats", headers=_headers(bob["api_key"])).json()
        assert stats["total"] == 0

        # Alice sees one task
        stats = client.post("/api/dashboard/stats", headers=_headers(alice["api_key"])).json()
        assert stats["total"] == 1
        assert stats["pending"] == 1


# ─── Task editing ─────────────────────────────────────────────


class TestTaskEditing:
    def _setup_project(self, client) -> tuple[str, dict]:
        reg = _create_user(client)
        h = _headers(reg["api_key"])
        resp = client.post("/api/project/create", headers=h, json={
            "name": "edit-test", "repo_url": "file:///tmp/repo",
        })
        return resp.json()["project_id"], h

    def test_edit_pending_task_description(self, client):
        project_id, h = self._setup_project(client)
        task_id = client.post("/api/task/create", headers=h, json={
            "project_id": project_id, "description": "Original",
        }).json()["task_id"]
        resp = client.patch("/api/task/edit", headers=h, json={
            "task_id": task_id, "description": "Updated",
        })
        assert resp.status_code == 200
        task = resp.json()["task"]
        assert task["input"]["description"] == "Updated"
        assert task["revision"] == 1

    def test_edit_task_params(self, client):
        project_id, h = self._setup_project(client)
        task_id = client.post("/api/task/create", headers=h, json={
            "project_id": project_id, "description": "Test",
        }).json()["task_id"]
        resp = client.patch("/api/task/edit", headers=h, json={
            "task_id": task_id, "params": {"branch": "feature-x"},
        })
        assert resp.status_code == 200
        assert resp.json()["task"]["input"]["params"]["branch"] == "feature-x"

    def test_edit_completed_task_fails(self, client):
        project_id, h = self._setup_project(client)
        task_id = client.post("/api/task/create", headers=h, json={
            "project_id": project_id, "description": "Done",
        }).json()["task_id"]
        client.post("/api/task/claim", headers=_agent_headers(), json={
            "agent_id": "dev-0", "agent_type": "dev", "project_id": project_id,
        })
        client.post("/api/task/status", headers=_agent_headers(), json={"task_id": task_id, "status": "in_progress"})
        client.post("/api/task/status", headers=_agent_headers(), json={"task_id": task_id, "status": "completed", "detail": "done"})
        resp = client.patch("/api/task/edit", headers=h, json={
            "task_id": task_id, "description": "Too late",
        })
        assert resp.status_code == 400

    def test_edit_increments_revision(self, client):
        project_id, h = self._setup_project(client)
        task_id = client.post("/api/task/create", headers=h, json={
            "project_id": project_id, "description": "Rev test",
        }).json()["task_id"]
        client.patch("/api/task/edit", headers=h, json={"task_id": task_id, "description": "Rev 1"})
        resp = client.patch("/api/task/edit", headers=h, json={"task_id": task_id, "description": "Rev 2"})
        assert resp.json()["task"]["revision"] == 2


# ─── Dev pool ─────────────────────────────────────────────────


class TestDevPool:
    def test_create_dev_slot(self, client):
        resp = client.post("/api/dev/create", headers=_admin_headers(), json={
            "model": "qwen3:8b", "provider": "ollama",
            "endpoint": "http://localhost:11434", "repo_url": "file:///tmp/repo",
        })
        assert resp.status_code == 200
        assert resp.json()["id"]
        assert resp.json()["model"] == "qwen3:8b"

    def test_list_dev_slots(self, client):
        client.post("/api/dev/create", headers=_admin_headers(), json={
            "model": "qwen3:8b", "provider": "ollama",
        })
        resp = client.get("/api/dev/list", headers=_admin_headers())
        assert resp.status_code == 200
        assert len(resp.json()["slots"]) == 1

    def test_update_dev_slot(self, client):
        slot = client.post("/api/dev/create", headers=_admin_headers(), json={
            "model": "qwen3:8b", "provider": "ollama",
        }).json()
        resp = client.patch(f"/api/dev/{slot['id']}", headers=_admin_headers(), json={
            "model": "claude-sonnet-4-6", "provider": "anthropic",
        })
        assert resp.status_code == 200
        assert resp.json()["model"] == "claude-sonnet-4-6"

    def test_delete_dev_slot(self, client):
        slot = client.post("/api/dev/create", headers=_admin_headers(), json={
            "model": "qwen3:8b", "provider": "ollama",
        }).json()
        resp = client.delete(f"/api/dev/{slot['id']}", headers=_admin_headers())
        assert resp.status_code == 200
        slots = client.get("/api/dev/list", headers=_admin_headers()).json()["slots"]
        assert len(slots) == 0

    def test_non_admin_cannot_manage_dev_slots(self, client):
        user = _create_user(client)
        resp = client.post("/api/dev/create", headers=_headers(user["api_key"]), json={
            "model": "qwen3:8b", "provider": "ollama",
        })
        assert resp.status_code == 403


# ─── Orchestrator user flow ──────────────────────────────────


class TestOrchestratorUserFlow:
    def test_create_orchestrator_task_via_api(self, client):
        reg = _create_user(client)
        h = _headers(reg["api_key"])
        project_id = client.post("/api/project/create", headers=h, json={
            "name": "orch-user", "repo_url": "file:///tmp/repo",
        }).json()["project_id"]
        resp = client.post("/api/task/create", headers=h, json={
            "project_id": project_id,
            "description": "Build complete auth system",
            "type": "orchestrator",
        })
        assert resp.status_code == 200
        task = client.post("/api/task/get", headers=h, json={
            "task_id": resp.json()["task_id"],
        }).json()["task"]
        assert task["type"] == "orchestrator"

    def test_default_type_is_dev(self, client):
        reg = _create_user(client)
        h = _headers(reg["api_key"])
        project_id = client.post("/api/project/create", headers=h, json={
            "name": "default-type", "repo_url": "file:///tmp/repo",
        }).json()["project_id"]
        resp = client.post("/api/task/create", headers=h, json={
            "project_id": project_id, "description": "Simple task",
        })
        task = client.post("/api/task/get", headers=h, json={
            "task_id": resp.json()["task_id"],
        }).json()["task"]
        assert task["type"] == "dev"


class TestHealthz:
    """Liveness probe — must answer without any credentials so container
    orchestrators can poll it."""

    def test_returns_ok_without_auth(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_reports_node_id(self, client):
        assert client.get("/healthz").json()["node_id"] == "test-node"

    def test_does_not_accept_api_keys_as_a_requirement(self, client):
        # Same answer with a bogus key: the probe must never depend on auth state.
        resp = client.get("/healthz", headers={"X-Api-Key": "not-a-real-key"})
        assert resp.status_code == 200

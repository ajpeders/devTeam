"""Entrypoint for the MyDevTeam daemon.

Usage: python -m daemon.main [--config config.yaml]
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets

import uvicorn

from daemon.agents.manager import AgentManager
from daemon.api.server import DaemonServer
from daemon.config import parse_config
from daemon.git.workspace import WorkspaceManager
from daemon.nats.sync import TaskSyncer
from daemon.tasks.models import Project, User
from daemon.tasks.store import TaskStore


def _ensure_admin(store: TaskStore, admin_api_key: str, logger) -> User:
    """Ensure admin user exists with the configured API key. Returns admin user."""
    existing = store.get_user_by_api_key(admin_api_key)
    if existing:
        if not existing.is_admin:
            existing.is_admin = True
            # Update in DB
            with store._session() as session:
                from daemon.tasks.store import UserRow
                row = session.query(UserRow).filter_by(id=str(existing.id)).first()
                if row:
                    row.is_admin = True
                    session.commit()
        return existing

    admin = User(name="admin", api_key=admin_api_key, is_admin=True)
    store.create_user(admin)
    logger.info("created admin user (id=%s)", admin.id)
    return admin


def _apply_admin_email_allowlist(store: TaskStore, logger) -> int:
    """Promote existing users whose email matches MYDEVTEAM_ADMIN_EMAILS to admin.

    Comma-separated, case-insensitive. Empty/unset env var = no-op (safe default —
    we never grant admin without an explicit allowlist). Returns the count promoted.
    """
    raw = os.environ.get("MYDEVTEAM_ADMIN_EMAILS", "")
    if not raw:
        return 0
    allowlist = {e.strip().lower() for e in raw.split(",") if e.strip()}
    if not allowlist:
        return 0
    from daemon.tasks.store import UserRow
    promoted = 0
    with store._session() as session:
        rows = session.query(UserRow).filter(UserRow.is_admin.is_(False)).all()
        for row in rows:
            if row.email and row.email.lower() in allowlist:
                row.is_admin = True
                promoted += 1
        if promoted:
            session.commit()
    if promoted:
        logger.info("MYDEVTEAM_ADMIN_EMAILS: promoted %d user(s) to admin", promoted)
    return promoted


def main():
    parser = argparse.ArgumentParser(description="devteam daemon")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("daemon")

    cfg = parse_config(args.config)
    logger.info("loaded config: node_id=%s", cfg.cluster.node_id)

    # Task store
    db_path = os.path.join(cfg.git.workspace_dir, "devteam.db")
    os.makedirs(cfg.git.workspace_dir, exist_ok=True)
    store = TaskStore(db_path)
    logger.info("task store opened at %s", db_path)

    # Ensure admin user exists. The admin key can come from YAML (cfg.api.admin_api_key)
    # OR the MYDEVTEAM_API_KEY env var — env wins when both are set so secrets can be
    # rotated without editing config files. If neither is set, fail-closed at startup:
    # we will not launch with no admin credential at all.
    admin_api_key = os.environ.get("MYDEVTEAM_API_KEY", "") or cfg.api.admin_api_key
    if not admin_api_key:
        logger.error(
            "no admin API key configured: set MYDEVTEAM_API_KEY in the environment "
            "or api.admin_api_key in config — refusing to start with unprotected admin "
            "endpoints",
        )
        raise SystemExit(1)
    admin = _ensure_admin(store, admin_api_key, logger)

    # Auto-elevate any users whose email matches MYDEVTEAM_ADMIN_EMAILS. Safe default:
    # empty list = nothing happens.
    _apply_admin_email_allowlist(store, logger)

    # NATS syncer (optional)
    nats_url = None
    if cfg.cluster.seeds:
        nats_url = cfg.cluster.seeds[0]
    syncer = TaskSyncer(nats_url, store, cfg.cluster.node_id)

    # Workspace manager
    workspace = WorkspaceManager(cfg.git.workspace_dir, cfg.git.default_remote)
    logger.info("workspace manager initialized")

    # Shared secret for agent-internal API auth
    agent_secret = secrets.token_urlsafe(32)

    # Resolve projects and spawn per-project agent managers
    api_address = os.environ.get("DEVTEAM_API_ADDRESS") or cfg.api.address
    project_configs = cfg.resolved_projects()
    agent_managers: list[AgentManager] = []

    for pc in project_configs:
        existing = store.get_project_by_name(pc.name)
        if existing:
            project_id = str(existing.id)
            # Assign to admin if unowned
            if not existing.user_id:
                existing.user_id = admin.id
                with store._session() as session:
                    from daemon.tasks.store import ProjectRow
                    row = session.query(ProjectRow).filter_by(id=str(existing.id)).first()
                    if row:
                        row.user_id = str(admin.id)
                        session.commit()
        else:
            project = Project(name=pc.name, repo_url=pc.repo_url, user_id=admin.id)
            store.create_project(project)
            project_id = str(project.id)
            logger.info("auto-created project %s (id=%s, owner=admin)", pc.name, project_id)

        if pc.agents:
            mgr = AgentManager(
                agent_configs=pc.agents,
                api_address=api_address,
                project_dir=os.getcwd(),
                log_dir=os.path.join(cfg.git.workspace_dir, "logs"),
                project_id=project_id,
                agent_secret=agent_secret,
            )
            agent_managers.append(mgr)

    # API server
    server = DaemonServer(
        store=store,
        syncer=syncer,
        workspace=workspace,
        node_id=cfg.cluster.node_id,
        api_address=api_address,
        agent_managers=agent_managers,
        agent_secret=agent_secret,
    )

    # Start all agent managers
    for mgr in agent_managers:
        mgr.start()
    if agent_managers:
        logger.info("started %d agent manager(s) across %d project(s)",
                     len(agent_managers), len(project_configs))

    # Parse host:port. The bind target is separate from the advertised address so a
    # container can listen on 0.0.0.0 while agents keep dialing localhost.
    #
    # Resolution order, most specific first. DEVTEAM_API_ADDRESS must keep moving the
    # bind (it is documented that way and predates bind_address): it feeds api_address
    # above, which is the final fallback here.
    bind_address = cfg.api.resolve_bind_address(api_address)
    if ":" in bind_address and not bind_address.startswith("/"):
        host, port_str = bind_address.rsplit(":", 1)
        host = host or "localhost"
        port = int(port_str)
    else:
        host, port = "localhost", 4223

    logger.info("API server starting on %s:%d", host, port)
    try:
        uvicorn.run(server.app, host=host, port=port, log_level="warning")
    finally:
        for mgr in agent_managers:
            mgr.stop()
        store.close()
        logger.info("daemon shut down")


if __name__ == "__main__":
    main()

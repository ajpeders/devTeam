"""Agent subprocess lifecycle manager — spawn, monitor, restart."""

from __future__ import annotations

import json
import logging
import os
import resource
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from daemon.config import AgentConfig

logger = logging.getLogger(__name__)

MODULE_MAP = {"review": "pr_manager", "orchestrator": "orchestrator"}


def _module_name(agent_type: str) -> str:
    return MODULE_MAP.get(agent_type, agent_type)


@dataclass
class AgentProcess:
    id: str
    type: str
    process: subprocess.Popen | None = None
    log_path: str = ""
    started_at: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    restarts: int = 0
    dead: bool = False


class DevPool:
    """Manages dynamic dev agent slots with mutable configs."""
    def __init__(self, store, agent_manager: "AgentManager"):
        self._store = store
        self._manager = agent_manager
        self._slot_to_agent: dict[str, str] = {}

    def assign_slot(self, slot_id: str, agent_id: str):
        self._slot_to_agent[slot_id] = agent_id

    def get_slot_for_agent(self, agent_id: str):
        for slot_id, aid in self._slot_to_agent.items():
            if aid == agent_id:
                return self._store.get_dev_slot(slot_id)
        return None


class AgentManager:
    """Spawns and monitors Python agent subprocesses."""

    MAX_RESTARTS = 3
    HEARTBEAT_TIMEOUT = 30  # seconds
    CHECK_INTERVAL = 10  # seconds

    def __init__(
        self,
        agent_configs: list[AgentConfig],
        api_address: str,
        project_dir: str = "",
        log_dir: str = "",
        python_path: str = "",
        project_id: str = "",
        agent_secret: str = "",
    ):
        self.agent_configs = agent_configs
        self.api_address = api_address
        self.project_dir = project_dir or os.getcwd()
        self.log_dir = log_dir or str(Path(self.project_dir) / "logs")
        self.python_path = python_path or self._find_python()
        self.project_id = project_id
        self.agent_secret = agent_secret

        self._agents: dict[str, AgentProcess] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self.dev_pool = None

    def _find_python(self) -> str:
        venv_python = Path(self.project_dir) / ".venv" / "bin" / "python3"
        if venv_python.exists():
            return str(venv_python)
        return "python3"

    def start(self):
        for cfg in self.agent_configs:
            for i in range(cfg.count):
                self._spawn_agent(cfg, i)

        self._monitor_thread = threading.Thread(target=self._health_loop, daemon=True)
        self._monitor_thread.start()

    def stop(self):
        self._stop_event.set()

        with self._lock:
            agents = list(self._agents.values())

        # SIGTERM all
        for a in agents:
            if a.process and a.process.poll() is None:
                try:
                    os.killpg(os.getpgid(a.process.pid), signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass

        # Wait up to 10s
        deadline = time.time() + 10
        while time.time() < deadline:
            if all(a.process is None or a.process.poll() is not None for a in agents):
                break
            time.sleep(0.1)

        # SIGKILL stragglers
        for a in agents:
            if a.process and a.process.poll() is None:
                try:
                    os.killpg(os.getpgid(a.process.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass

    def record_heartbeat(self, agent_id: str):
        with self._lock:
            if agent_id in self._agents:
                self._agents[agent_id].last_heartbeat = time.time()

    def list_statuses(self) -> list[dict]:
        with self._lock:
            result = []
            for a in self._agents.values():
                status = "dead" if a.dead else "running"
                if a.process and a.process.poll() is not None and not a.dead:
                    status = "exited"
                cfg = self.get_config_for_type(a.type)
                result.append({
                    "id": a.id,
                    "type": a.type,
                    "status": status,
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(a.started_at)),
                    "last_heart": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(a.last_heartbeat)),
                    "restarts": a.restarts,
                    "llm_model": cfg.llm.primary.model if cfg else "",
                    "max_memory_mb": cfg.max_memory_mb if cfg else None,
                    "max_cpu_percent": cfg.max_cpu_percent if cfg else None,
                    "log_path": a.log_path,
                })
            return result

    def get_agent_log(self, agent_id: str, tail_lines: int = 100) -> tuple[str, int] | None:
        """Return (content, total_lines) for the agent's log file, or None if the agent is not registered."""
        log_path: str | None = None
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent:
                log_path = agent.log_path

        if log_path is None:
            return None

        if not log_path:
            log_path = str(Path(self.log_dir) / f"agent-{agent_id}.log")

        try:
            with open(log_path) as f:
                lines = f.readlines()
            total = len(lines)
            return "".join(lines[-tail_lines:]), total
        except FileNotFoundError:
            return "", 0

    def get_config_for_type(self, agent_type: str) -> AgentConfig | None:
        for cfg in self.agent_configs:
            if cfg.type == agent_type:
                return cfg
        return None

    def get_dev_slot_for_agent(self, agent_id: str):
        if hasattr(self, 'dev_pool') and self.dev_pool:
            return self.dev_pool.get_slot_for_agent(agent_id)
        return None

    def _make_preexec_fn(self, cfg: AgentConfig):
        """Build preexec_fn at spawn time to avoid closure issues."""
        if os.getuid() == 0:
            return lambda: None
        return lambda: self._set_rlimits(cfg)

    def _set_rlimits(self, cfg: AgentConfig):
        """Apply resource limits to the calling process (used as preexec_fn)."""
        try:
            if cfg.max_memory_mb:
                resource.setrlimit(
                    resource.RLIMIT_AS,
                    (cfg.max_memory_mb * 1024 * 1024, resource.RLIM_INFINITY),
                )
            if cfg.max_cpu_percent:
                resource.setrlimit(resource.RLIMIT_CPU, (cfg.max_cpu_percent, resource.RLIM_INFINITY))
        except (ValueError, OSError):
            pass  # Non-fatal; limits are advisory

    def _spawn_agent(self, cfg: AgentConfig, index: int):
        agent_id = f"{cfg.type}-{index}"

        llm_config = {
            "primary": {
                "provider": cfg.llm.primary.provider,
                "model": cfg.llm.primary.model,
                "endpoint": cfg.llm.primary.endpoint,
                "api_key": cfg.llm.primary.api_key,
            },
            "timeout": cfg.llm.timeout_seconds(),
        }
        if cfg.llm.fallback:
            llm_config["fallback"] = {
                "provider": cfg.llm.fallback.provider,
                "model": cfg.llm.fallback.model,
                "endpoint": cfg.llm.fallback.endpoint,
                "api_key": cfg.llm.fallback.api_key,
            }

        env = os.environ.copy()
        env["DEVTEAM_SOCKET"] = self.api_address
        env["DEVTEAM_AGENT_ID"] = agent_id
        env["DEVTEAM_AGENT_TYPE"] = cfg.type
        env["DEVTEAM_LLM_CONFIG"] = json.dumps(llm_config)
        if self.agent_secret:
            env["DEVTEAM_AGENT_SECRET"] = self.agent_secret
        if self.project_id:
            env["DEVTEAM_PROJECT_ID"] = self.project_id

        module = _module_name(cfg.type)
        cmd = [self.python_path, "-m", f"agents.{module}.agent"]

        Path(self.log_dir).mkdir(parents=True, exist_ok=True)
        log_path = str(Path(self.log_dir) / f"agent-{agent_id}.log")
        log_file = open(log_path, "a")

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=self.project_dir,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                preexec_fn=self._make_preexec_fn(cfg),
            )
        except Exception:
            log_file.close()
            raise

        agent = AgentProcess(
            id=agent_id,
            type=cfg.type,
            process=proc,
            log_path=log_path,
        )

        with self._lock:
            existing = self._agents.get(agent_id)
            if existing:
                agent.restarts = existing.restarts
            self._agents[agent_id] = agent

        logger.info("spawned agent %s (pid %d)", agent_id, proc.pid)

    def _health_loop(self):
        while not self._stop_event.is_set():
            self._stop_event.wait(self.CHECK_INTERVAL)
            if self._stop_event.is_set():
                break
            self._check_health()

    def _check_health(self):
        with self._lock:
            agents_snapshot = list(self._agents.items())

        now = time.time()
        for agent_id, agent in agents_snapshot:
            if agent.dead:
                continue

            stale = (now - agent.last_heartbeat) > self.HEARTBEAT_TIMEOUT
            exited = agent.process and agent.process.poll() is not None

            if not stale and not exited:
                continue

            # Kill stale process
            if agent.process and agent.process.poll() is None:
                try:
                    os.killpg(os.getpgid(agent.process.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass

            with self._lock:
                agent.restarts += 1
                if agent.restarts > self.MAX_RESTARTS:
                    agent.dead = True
                    logger.error("agent %s exceeded max restarts, marking dead", agent_id)
                    continue

            # Find config and restart
            cfg = next((c for c in self.agent_configs if c.type == agent.type), None)
            if cfg:
                idx = int(agent_id.split("-")[-1])
                logger.info("restarting agent %s (attempt %d)", agent_id, agent.restarts)
                try:
                    self._spawn_agent(cfg, idx)
                except Exception:
                    logger.exception("failed to restart agent %s", agent_id)
                    with self._lock:
                        agent.dead = True
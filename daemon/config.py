"""YAML config parsing with pydantic validation."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class LLMProvider(BaseModel):
    provider: str
    model: str
    endpoint: str = ""
    api_key: str = ""


class LLMConfig(BaseModel):
    primary: LLMProvider
    fallback: LLMProvider | None = None
    timeout: str = "120s"

    def timeout_seconds(self) -> int:
        t = self.timeout
        if t.endswith("s"):
            return int(t[:-1])
        if t.endswith("m"):
            return int(t[:-1]) * 60
        return int(t)


class AgentConfig(BaseModel):
    type: str
    count: int = 1
    llm: LLMConfig
    max_memory_mb: int | None = None  # RLIMIT_AS in MB, None = no limit
    max_cpu_percent: int | None = None  # RLIMIT_CPU percent (of one core), None = no limit


class ProjectConfig(BaseModel):
    name: str
    repo_url: str
    agents: list[AgentConfig] = Field(default_factory=list)


class ClusterConfig(BaseModel):
    node_id: str
    seeds: list[str] = Field(default_factory=list)


class APIConfig(BaseModel):
    # Address agents dial back on (exported as DEVTEAM_SOCKET). Must be reachable
    # from wherever agent processes run.
    address: str = "localhost:4223"
    # Interface uvicorn binds to. Empty = bind whatever `address` says. These differ
    # in containers: bind 0.0.0.0 so the published port works, while agents inside
    # the container still reach the daemon on localhost.
    bind_address: str = ""
    admin_api_key: str = ""

    def effective_bind_address(self) -> str:
        return self.bind_address or self.address

    def resolve_bind_address(self, api_address: str = "", env: Mapping[str, str] | None = None) -> str:
        """Where uvicorn should listen, most specific source first.

        DEVTEAM_BIND_ADDRESS > api.bind_address > the advertised address. That last
        fallback keeps DEVTEAM_API_ADDRESS moving the bind, which is how it was
        documented before bind_address existed — two mechanisms for the same job
        landed independently, and both must keep working.
        """
        env = os.environ if env is None else env
        return env.get("DEVTEAM_BIND_ADDRESS") or self.bind_address or api_address or self.address


class GitConfig(BaseModel):
    default_remote: str = ""
    workspace_dir: str = "/tmp/mydevteam-workspaces"


class DockerConfig(BaseModel):
    registry: str = ""


class DeployTarget(BaseModel):
    name: str
    host: str
    compose_file: str = "docker-compose.yml"


class DeployConfig(BaseModel):
    method: str = "docker-compose"
    targets: list[DeployTarget] = Field(default_factory=list)
    default_target: str = "local"


class Config(BaseModel):
    cluster: ClusterConfig
    api: APIConfig = Field(default_factory=APIConfig)
    projects: list[ProjectConfig] = Field(default_factory=list)
    # Legacy: top-level agents (auto-wrapped into a default project)
    agents: list[AgentConfig] = Field(default_factory=list)
    git: GitConfig = Field(default_factory=GitConfig)
    docker: DockerConfig = Field(default_factory=DockerConfig)
    deploy: DeployConfig = Field(default_factory=DeployConfig)

    def resolved_projects(self) -> list[ProjectConfig]:
        """Return projects list, migrating legacy top-level agents if needed."""
        if self.projects:
            return self.projects
        if self.agents:
            return [ProjectConfig(
                name="default",
                repo_url=self.git.default_remote,
                agents=self.agents,
            )]
        return []


def parse_config(path: str | Path) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)

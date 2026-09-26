from __future__ import annotations

import json
import os
import stat
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COMMANDER_", env_file=".env", extra="ignore")

    agent_token: str = ""
    agent_token_file: str = "~/.remote-mcp-commander/agent-token"
    agent_tokens_json: str = "{}"
    agent_policies_json: str = "{}"
    control_token: str = ""
    registry_path: str = "~/.remote-mcp-commander/registry.json"
    enrollment_ttl_s: int = Field(default=300, ge=30, le=3600)
    bind_host: str = "127.0.0.1"
    bind_port: int = 8765
    request_timeout_s: float = 15.0

    agent_id: str = "server-01"
    gateway_ws: str = "ws://127.0.0.1:8765/ws/agent/server-01"
    allowed_executables: str = "echo,hostname,whoami,uptime"
    max_output_bytes: int = 65_536
    exec_timeout_s: float = 10.0

    gateway_http: str = "http://127.0.0.1:8765"
    mcp_transport: Literal["stdio", "streamable-http"] = "stdio"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8766
    mcp_path: str = "/mcp"
    mcp_token: str = ""
    mcp_issuer_url: str = "http://127.0.0.1:8766"
    mcp_resource_url: str = "http://127.0.0.1:8766/mcp"
    mcp_client_id: str = "remote-mcp-client"
    mcp_scope: str = "commander:use"
    mcp_gateway_timeout_s: float = 20.0

    @property
    def executable_allowlist(self) -> set[str]:
        return {item.strip() for item in self.allowed_executables.split(",") if item.strip()}

    @property
    def agent_tokens(self) -> dict[str, str]:
        data = json.loads(self.agent_tokens_json)
        if not isinstance(data, dict):
            raise ValueError("COMMANDER_AGENT_TOKENS_JSON must be a JSON object")
        return {str(key): str(value) for key, value in data.items()}

    @property
    def agent_policies(self) -> dict[str, set[str]]:
        data = json.loads(self.agent_policies_json)
        if not isinstance(data, dict):
            raise ValueError("COMMANDER_AGENT_POLICIES_JSON must be a JSON object")
        policies: dict[str, set[str]] = {}
        for agent_id, executables in data.items():
            if not isinstance(executables, list):
                raise ValueError(f"policy for {agent_id} must be a JSON array")
            policies[str(agent_id)] = {str(item) for item in executables}
        return policies

    @property
    def registry_file(self) -> Path:
        return Path(self.registry_path).expanduser()

    @property
    def agent_token_path(self) -> Path:
        return Path(self.agent_token_file).expanduser()

    def token_for_agent(self, agent_id: str) -> str | None:
        token = self.agent_tokens.get(agent_id)
        if token:
            return token
        return self.agent_token or None

    def load_agent_token(self) -> str:
        if self.agent_token:
            return self.agent_token
        path = self.agent_token_path
        if not path.exists():
            return ""
        if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError("Agent token file must not be group/world accessible")
        return path.read_text(encoding="utf-8").strip()

    @staticmethod
    def _credential_is_unsafe(value: str) -> bool:
        return len(value) < 16 or value.startswith("change-" + "me-")

    def validate_gateway_security(self) -> None:
        if self._credential_is_unsafe(self.control_token):
            raise ValueError("replace the placeholder Gateway control credential before startup")
        static_agent_credentials = [
            value for value in [self.agent_token, *self.agent_tokens.values()] if value
        ]
        if any(self._credential_is_unsafe(value) for value in static_agent_credentials):
            raise ValueError("replace all configured static Agent credentials before startup")

    def validate_agent_security(self, token: str) -> None:
        if self._credential_is_unsafe(token):
            raise ValueError("enroll the Agent or configure a non-placeholder credential")

    def validate_mcp_gateway_security(self) -> None:
        if self._credential_is_unsafe(self.control_token):
            raise ValueError("configure a non-placeholder Gateway control credential for MCP")

    def validate_mcp_http_security(self) -> None:
        if self.mcp_transport != "streamable-http":
            return
        if self._credential_is_unsafe(self.mcp_token):
            raise ValueError("set a non-placeholder COMMANDER_MCP_TOKEN for Streamable HTTP")


@lru_cache
def get_settings() -> Settings:
    return Settings()

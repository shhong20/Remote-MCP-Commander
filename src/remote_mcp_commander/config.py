from __future__ import annotations

import json
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COMMANDER_", env_file=".env", extra="ignore")

    agent_token: str = Field(min_length=16)
    agent_tokens_json: str = "{}"
    agent_policies_json: str = "{}"
    control_token: str = Field(min_length=16)
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

    def token_for_agent(self, agent_id: str) -> str:
        return self.agent_tokens.get(agent_id, self.agent_token)

    @staticmethod
    def _credential_is_unsafe(value: str) -> bool:
        return len(value) < 16 or value.startswith("change-" + "me-")

    def validate_gateway_security(self) -> None:
        credentials = [self.agent_token, self.control_token, *self.agent_tokens.values()]
        if any(self._credential_is_unsafe(value) for value in credentials):
            raise ValueError("replace all placeholder Gateway/Agent credentials before startup")

    def validate_agent_security(self) -> None:
        if self._credential_is_unsafe(self.agent_token):
            raise ValueError("replace the placeholder Agent credential before startup")

    def validate_mcp_http_security(self) -> None:
        if self.mcp_transport != "streamable-http":
            return
        if self._credential_is_unsafe(self.mcp_token):
            raise ValueError("set a non-placeholder COMMANDER_MCP_TOKEN for Streamable HTTP")


@lru_cache
def get_settings() -> Settings:
    return Settings()

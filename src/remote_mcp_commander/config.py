from __future__ import annotations

import json
import os
import secrets
import stat
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COMMANDER_", env_file=".env", extra="ignore")

    agent_token: str = ""
    agent_token_file: str = "~/.remote-mcp-commander/agent-token"
    agent_tokens_json: str = "{}"
    agent_policies_json: str = "{}"
    control_token: str = ""
    approval_admin_token: str = ""
    approval_ttl_s: int = Field(default=60, ge=15, le=600)
    audit_path: str = "~/.remote-mcp-commander/audit.jsonl"
    audit_fsync: bool = False
    audit_query_max_scan_bytes: int = Field(default=2_097_152, ge=65_536, le=16_777_216)
    registry_path: str = "~/.remote-mcp-commander/registry.json"
    enrollment_ttl_s: int = Field(default=300, ge=30, le=3600)
    bind_host: str = "127.0.0.1"
    bind_port: int = 8765
    request_timeout_s: float = 15.0

    agent_id: str = Field(default="server-01", pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    gateway_ws: str = "ws://127.0.0.1:8765/ws/agent/server-01"
    allowed_executables: str = "echo,hostname,whoami,uptime"
    max_output_bytes: int = Field(default=65_536, ge=1_024, le=262_144)
    exec_timeout_s: float = 10.0
    session_timeout_s: float = Field(default=300.0, ge=1.0, le=3600.0)
    session_max_active: int = Field(default=4, ge=1, le=32)
    session_history_limit: int = Field(default=100, ge=10, le=200)
    pty_allowed_executables: str = ""
    pty_agent_policies_json: str = "{}"
    pty_timeout_s: float = Field(default=900.0, ge=1.0, le=3600.0)
    pty_max_active: int = Field(default=1, ge=1, le=4)
    pty_input_max_bytes: int = Field(default=16_384, ge=1, le=65_536)
    allowed_roots_json: str = "[]"
    file_max_bytes: int = Field(default=1_048_576, ge=1, le=1_048_576)

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
    def pty_executable_allowlist(self) -> set[str]:
        return {
            item.strip() for item in self.pty_allowed_executables.split(",") if item.strip()
        }

    @property
    def pty_agent_policies(self) -> dict[str, set[str]]:
        data = json.loads(self.pty_agent_policies_json)
        if not isinstance(data, dict):
            raise ValueError("COMMANDER_PTY_AGENT_POLICIES_JSON must be a JSON object")
        policies: dict[str, set[str]] = {}
        for agent_id, executables in data.items():
            if not isinstance(executables, list):
                raise ValueError(f"PTY policy for {agent_id} must be a JSON array")
            policies[str(agent_id)] = {str(item) for item in executables}
        return policies

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
    def allowed_roots(self) -> list[str]:
        data = json.loads(self.allowed_roots_json)
        if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
            raise ValueError("COMMANDER_ALLOWED_ROOTS_JSON must be a JSON string array")
        return data

    @property
    def registry_file(self) -> Path:
        return Path(self.registry_path).expanduser()

    @property
    def audit_file(self) -> Path:
        return Path(self.audit_path).expanduser()

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

    @staticmethod
    def _require_secure_remote_url(value: str, secure_scheme: str, loopback_scheme: str) -> None:
        parsed = urlparse(value)
        loopback_hosts = {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme == secure_scheme:
            return
        if parsed.scheme == loopback_scheme and parsed.hostname in loopback_hosts:
            return
        raise ValueError(
            f"remote URL must use {secure_scheme}://, except loopback may use {loopback_scheme}://"
        )

    def validate_gateway_security(self) -> None:
        if self._credential_is_unsafe(self.control_token):
            raise ValueError("replace the placeholder Gateway control credential before startup")
        if self.approval_admin_token:
            if self._credential_is_unsafe(self.approval_admin_token):
                raise ValueError("replace the placeholder approval-admin credential before startup")
            privileged_credentials = [self.control_token, self.mcp_token, self.agent_token]
            privileged_credentials.extend(self.agent_tokens.values())
            if any(
                value and secrets.compare_digest(value, self.approval_admin_token)
                for value in privileged_credentials
            ):
                raise ValueError("approval-admin credential must be unique")
        static_agent_credentials = [
            value for value in [self.agent_token, *self.agent_tokens.values()] if value
        ]
        if any(self._credential_is_unsafe(value) for value in static_agent_credentials):
            raise ValueError("replace all configured static Agent credentials before startup")

    def validate_agent_security(self, token: str) -> None:
        if self._credential_is_unsafe(token):
            raise ValueError("enroll the Agent or configure a non-placeholder credential")
        if self.session_history_limit < self.session_max_active:
            raise ValueError("session history limit must be >= max active sessions")
        self._require_secure_remote_url(self.gateway_ws, "wss", "ws")
        parsed = urlparse(self.gateway_ws)
        expected_path = f"/ws/agent/{self.agent_id}"
        if parsed.path != expected_path or parsed.query or parsed.fragment:
            raise ValueError("Agent Gateway WebSocket URL does not match configured Agent ID")

    def validate_mcp_gateway_security(self) -> None:
        if self._credential_is_unsafe(self.control_token):
            raise ValueError("configure a non-placeholder Gateway control credential for MCP")
        self._require_secure_remote_url(self.gateway_http, "https", "http")

    def validate_mcp_http_security(self) -> None:
        if self.mcp_transport != "streamable-http":
            return
        if self._credential_is_unsafe(self.mcp_token):
            raise ValueError("set a non-placeholder COMMANDER_MCP_TOKEN for Streamable HTTP")
        if self.mcp_host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                "MCP Streamable HTTP must bind to loopback; terminate TLS at a reverse proxy"
            )
        self._require_secure_remote_url(self.mcp_resource_url, "https", "http")
        self._require_secure_remote_url(self.mcp_issuer_url, "https", "http")


@lru_cache
def get_settings() -> Settings:
    return Settings()

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COMMANDER_", env_file=".env", extra="ignore")

    agent_token: str = Field(min_length=16)
    control_token: str = Field(min_length=16)
    bind_host: str = "127.0.0.1"
    bind_port: int = 8765
    request_timeout_s: float = 15.0

    agent_id: str = "server-01"
    gateway_ws: str = "ws://127.0.0.1:8765/ws/agent/server-01"
    allowed_executables: str = "echo,hostname,whoami,uptime"
    max_output_bytes: int = 65_536
    exec_timeout_s: float = 10.0

    @property
    def executable_allowlist(self) -> set[str]:
        return {item.strip() for item in self.allowed_executables.split(",") if item.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()

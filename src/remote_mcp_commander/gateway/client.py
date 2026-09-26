from __future__ import annotations

from typing import Any

import httpx

from remote_mcp_commander.config import Settings
from remote_mcp_commander.protocol import (
    AgentInfo,
    AgentList,
    CommandResult,
    PingResponse,
)


class GatewayAPIError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"gateway request failed ({status_code}): {detail}")
        self.status_code = status_code
        self.detail = detail


class GatewayClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {self.settings.control_token}"}
        async with httpx.AsyncClient(
            base_url=self.settings.gateway_http,
            headers=headers,
            timeout=self.settings.mcp_gateway_timeout_s,
            transport=self.transport,
        ) as client:
            response = await client.request(method, path, json=json_body)

        if response.is_error:
            try:
                payload = response.json()
                detail = str(payload.get("detail", "gateway error"))
            except ValueError:
                detail = "gateway error"
            raise GatewayAPIError(response.status_code, detail)
        return response.json()

    async def list_devices(self) -> AgentList:
        payload = await self._request("GET", "/api/v1/agents")
        return AgentList.model_validate(payload)

    async def device_info(self, agent_id: str) -> AgentInfo:
        payload = await self._request("GET", f"/api/v1/agents/{agent_id}")
        return AgentInfo.model_validate(payload)

    async def ping_device(self, agent_id: str) -> PingResponse:
        payload = await self._request("POST", f"/api/v1/agents/{agent_id}/ping")
        return PingResponse.model_validate(payload)

    async def execute(self, agent_id: str, argv: list[str]) -> CommandResult:
        payload = await self._request(
            "POST",
            f"/api/v1/agents/{agent_id}/execute",
            json_body={"argv": argv},
        )
        return CommandResult.model_validate(payload)

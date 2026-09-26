import httpx
import pytest

from remote_mcp_commander.config import Settings
from remote_mcp_commander.gateway.client import GatewayAPIError, GatewayClient


def make_settings() -> Settings:
    return Settings(
        agent_token="agent-placeholder-value",
        control_token="control-placeholder-value",
        gateway_http="http://gateway.test",
    )


@pytest.mark.asyncio
async def test_list_devices_parses_gateway_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("Bearer ")
        return httpx.Response(
            200,
            json={
                "agents": [
                    {
                        "agent_id": "server-01",
                        "hostname": "worker-01",
                        "platform": "Linux",
                        "version": "0.1.0",
                        "connected_at": "2026-09-26T04:00:00Z",
                        "last_seen": "2026-09-26T04:01:00Z",
                    }
                ]
            },
        )

    client = GatewayClient(
        make_settings(),
        transport=httpx.MockTransport(handler),
    )
    result = await client.list_devices()
    assert result.agents[0].agent_id == "server-01"
    assert result.agents[0].hostname == "worker-01"


@pytest.mark.asyncio
async def test_gateway_error_preserves_status_and_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "policy denied"})

    client = GatewayClient(
        make_settings(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GatewayAPIError) as exc_info:
        await client.execute("server-01", ["whoami"])
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "policy denied"

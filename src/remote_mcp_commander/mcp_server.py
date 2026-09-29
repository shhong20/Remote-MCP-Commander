from __future__ import annotations

import secrets

from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl

from remote_mcp_commander.config import Settings, get_settings
from remote_mcp_commander.gateway.client import GatewayClient
from remote_mcp_commander.protocol import (
    AgentInfo,
    CommandResult,
    FileReadResult,
    FileWriteResult,
    PingResponse,
)


class StaticTokenVerifier(TokenVerifier):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def verify_token(self, token: str) -> AccessToken | None:
        expected = self.settings.mcp_token
        if not expected or not secrets.compare_digest(token, expected):
            return None
        return AccessToken(
            token=token,
            client_id=self.settings.mcp_client_id,
            scopes=[self.settings.mcp_scope],
            resource=self.settings.mcp_resource_url,
        )


def build_mcp(settings: Settings) -> MCPServer:
    settings.validate_mcp_gateway_security()
    kwargs: dict[str, object] = {
        "instructions": (
            "Operate only devices explicitly requested by the user. "
            "Execution uses argv arrays and is still subject to gateway and agent policy."
        )
    }
    if settings.mcp_transport == "streamable-http":
        settings.validate_mcp_http_security()
        kwargs.update(
            token_verifier=StaticTokenVerifier(settings),
            auth=AuthSettings(
                issuer_url=AnyHttpUrl(settings.mcp_issuer_url),
                resource_server_url=AnyHttpUrl(settings.mcp_resource_url),
                required_scopes=[settings.mcp_scope],
                validate_token_resource=True,
            ),
        )

    server = MCPServer("Remote MCP Commander", **kwargs)

    @server.tool()
    async def list_devices() -> list[AgentInfo]:
        """List currently connected remote devices and their metadata."""
        result = await GatewayClient(settings).list_devices()
        return result.agents

    @server.tool()
    async def device_info(agent_id: str) -> AgentInfo:
        """Return metadata for one connected device."""
        return await GatewayClient(settings).device_info(agent_id)

    @server.tool()
    async def ping_device(agent_id: str) -> PingResponse:
        """Measure an actual gateway-to-agent round trip for a connected device."""
        return await GatewayClient(settings).ping_device(agent_id)

    @server.tool()
    async def execute(agent_id: str, argv: list[str]) -> CommandResult:
        """Execute an argv command on a device subject to gateway and agent policies."""
        return await GatewayClient(settings).execute(agent_id, argv)

    @server.tool()
    async def read_file(
        agent_id: str, path: str, offset: int = 0, max_bytes: int = 65_536
    ) -> FileReadResult:
        """Read a bounded text chunk from an absolute path inside Agent allowed roots."""
        return await GatewayClient(settings).read_file(
            agent_id, path, offset=offset, max_bytes=max_bytes
        )

    @server.tool()
    async def write_file(
        agent_id: str,
        path: str,
        content: str,
        overwrite: bool = False,
        expected_sha256: str | None = None,
    ) -> FileWriteResult:
        """Write text inside Agent allowed roots. Read first before overwriting an existing file."""
        return await GatewayClient(settings).write_file(
            agent_id,
            path,
            content,
            overwrite=overwrite,
            expected_sha256=expected_sha256,
        )

    return server


def run() -> None:
    settings = get_settings()
    server = build_mcp(settings)
    if settings.mcp_transport == "stdio":
        server.run()
        return

    server.run(
        transport="streamable-http",
        host=settings.mcp_host,
        port=settings.mcp_port,
        streamable_http_path=settings.mcp_path,
        stateless_http=True,
    )


if __name__ == "__main__":
    run()

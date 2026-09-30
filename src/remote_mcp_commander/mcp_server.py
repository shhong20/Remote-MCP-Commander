from __future__ import annotations

import secrets
from urllib.parse import urlparse

from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from remote_mcp_commander.config import Settings, get_settings
from remote_mcp_commander.gateway.client import GatewayClient
from remote_mcp_commander.oauth import OAuthTokenVerifier
from remote_mcp_commander.protocol import (
    AgentInfo,
    CommandResult,
    CommandSessionDiscardResult,
    CommandSessionOutput,
    CommandSessionSnapshot,
    DirectoryListResult,
    FileInfoResult,
    FileReadResult,
    FileRootListResult,
    FileWriteResult,
    GitStatusResult,
    PingResponse,
    PortLookupResult,
    ProcessListResult,
    ProcessTerminateResult,
    PtySessionDiscardResult,
    PtySessionInputResult,
    PtySessionOutput,
    PtySessionResizeResult,
    PtySessionSnapshot,
    ServiceAction,
    ServiceActionResult,
    ServiceLogsResult,
    ServiceStatusResult,
    SystemHealthResult,
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
            "Execution uses argv arrays and is still subject to gateway and agent policy. "
            "Mutation tools require an externally issued, one-use approval and cannot self-approve."
        )
    }
    if settings.mcp_transport == "streamable-http":
        settings.validate_mcp_http_security()
        kwargs.update(
            token_verifier=(
                OAuthTokenVerifier(settings)
                if settings.mcp_auth_mode == "oauth"
                else StaticTokenVerifier(settings)
            ),
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
    async def system_health(agent_id: str) -> SystemHealthResult:
        """Read bounded CPU, memory, load, uptime, swap, and root-disk health metrics."""
        return await GatewayClient(settings).system_health(agent_id)

    @server.tool()
    async def lookup_port(agent_id: str, port: int) -> PortLookupResult:
        """Find listening processes for one TCP port without exposing command lines."""
        return await GatewayClient(settings).lookup_port(agent_id, port)

    @server.tool()
    async def service_logs(agent_id: str, unit: str, lines: int = 100) -> ServiceLogsResult:
        """Read bounded recent journal entries for one validated systemd unit."""
        return await GatewayClient(settings).service_logs(agent_id, unit, lines=lines)

    @server.tool()
    async def git_status(agent_id: str, path: str) -> GitStatusResult:
        """Read bounded porcelain-v2 status for a normal Git repo inside Agent allowed roots."""
        return await GatewayClient(settings).git_status(agent_id, path)

    @server.tool()
    async def execute(agent_id: str, argv: list[str]) -> CommandResult:
        """Execute an argv command on a device subject to gateway and agent policies."""
        return await GatewayClient(settings).execute(agent_id, argv)

    @server.tool()
    async def start_command(agent_id: str, argv: list[str]) -> CommandSessionSnapshot:
        """Start a bounded safe-profile command session and return its session ID."""
        return await GatewayClient(settings).start_command_session(agent_id, argv)

    @server.tool()
    async def command_status(agent_id: str, session_id: str) -> CommandSessionSnapshot:
        """Read the latest state and bounded output for a command session."""
        return await GatewayClient(settings).command_session_status(agent_id, session_id)

    @server.tool()
    async def cancel_command(agent_id: str, session_id: str) -> CommandSessionSnapshot:
        """Cancel a running command session owned by the current Agent connection."""
        return await GatewayClient(settings).cancel_command_session(agent_id, session_id)

    @server.tool()
    async def command_output(
        agent_id: str,
        session_id: str,
        stdout_offset: int = 0,
        stderr_offset: int = 0,
        max_chars: int = 8192,
    ) -> CommandSessionOutput:
        """Read only new bounded output from a command session using character cursors."""
        return await GatewayClient(settings).command_session_output(
            agent_id,
            session_id,
            stdout_offset=stdout_offset,
            stderr_offset=stderr_offset,
            max_chars=max_chars,
        )

    @server.tool()
    async def discard_command(agent_id: str, session_id: str) -> CommandSessionDiscardResult:
        """Discard a completed command session from Agent history."""
        return await GatewayClient(settings).discard_command_session(agent_id, session_id)

    @server.tool()
    async def start_pty(
        agent_id: str,
        argv: list[str],
        approval_id: str,
        approval_secret: str,
        columns: int = 80,
        rows: int = 24,
    ) -> PtySessionSnapshot:
        """Start an approved interactive POSIX PTY under separate Agent and Gateway policy."""
        return await GatewayClient(settings).start_pty_session(
            agent_id,
            argv,
            approval_id,
            approval_secret,
            columns=columns,
            rows=rows,
        )

    @server.tool()
    async def pty_status(agent_id: str, session_id: str) -> PtySessionSnapshot:
        """Read state and retained output for an approved PTY session."""
        return await GatewayClient(settings).pty_session_status(agent_id, session_id)

    @server.tool()
    async def write_pty(agent_id: str, session_id: str, data: str) -> PtySessionInputResult:
        """Write bounded UTF-8 input to a running approved PTY session."""
        return await GatewayClient(settings).write_pty_input(agent_id, session_id, data)

    @server.tool()
    async def resize_pty(
        agent_id: str, session_id: str, columns: int, rows: int
    ) -> PtySessionResizeResult:
        """Resize a running approved PTY session."""
        return await GatewayClient(settings).resize_pty_session(
            agent_id, session_id, columns=columns, rows=rows
        )

    @server.tool()
    async def pty_output(
        agent_id: str,
        session_id: str,
        offset: int = 0,
        max_chars: int = 8192,
    ) -> PtySessionOutput:
        """Read only new bounded PTY output using a character cursor."""
        return await GatewayClient(settings).pty_session_output(
            agent_id, session_id, offset=offset, max_chars=max_chars
        )

    @server.tool()
    async def cancel_pty(agent_id: str, session_id: str) -> PtySessionSnapshot:
        """Cancel a running PTY session owned by the current Agent connection."""
        return await GatewayClient(settings).cancel_pty_session(agent_id, session_id)

    @server.tool()
    async def discard_pty(agent_id: str, session_id: str) -> PtySessionDiscardResult:
        """Discard a completed PTY session from Agent history."""
        return await GatewayClient(settings).discard_pty_session(agent_id, session_id)

    @server.tool()
    async def list_file_roots(agent_id: str) -> FileRootListResult:
        """List the absolute filesystem roots explicitly allowed by the Agent."""
        return await GatewayClient(settings).list_file_roots(agent_id)

    @server.tool()
    async def list_directory(agent_id: str, path: str, limit: int = 200) -> DirectoryListResult:
        """List one directory inside allowed roots without recursive traversal."""
        return await GatewayClient(settings).list_directory(agent_id, path, limit=limit)

    @server.tool()
    async def file_info(agent_id: str, path: str) -> FileInfoResult:
        """Read lstat-style metadata for one path without following the final symlink."""
        return await GatewayClient(settings).file_info(agent_id, path)

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

    @server.tool()
    async def list_processes(agent_id: str, limit: int = 100) -> ProcessListResult:
        """List process metadata without exposing command-line arguments or environments."""
        return await GatewayClient(settings).list_processes(agent_id, limit=limit)

    @server.tool()
    async def service_status(agent_id: str, unit: str) -> ServiceStatusResult:
        """Read systemd service state using a fixed systemctl show query."""
        return await GatewayClient(settings).service_status(agent_id, unit)

    @server.tool()
    async def terminate_process(
        agent_id: str,
        pid: int,
        expected_create_time_ms: int,
        approval_id: str,
        approval_secret: str,
    ) -> ProcessTerminateResult:
        """Send SIGTERM only after consuming an externally issued one-use approval."""
        return await GatewayClient(settings).terminate_process(
            agent_id,
            pid,
            expected_create_time_ms,
            approval_id,
            approval_secret,
        )

    @server.tool()
    async def service_action(
        agent_id: str,
        unit: str,
        action: ServiceAction,
        approval_id: str,
        approval_secret: str,
    ) -> ServiceActionResult:
        """Start, stop, or restart one systemd unit with a matching one-use approval."""
        return await GatewayClient(settings).service_action(
            agent_id, unit, action, approval_id, approval_secret
        )

    return server


def http_transport_security(settings: Settings) -> TransportSecuritySettings:
    """Keep DNS-rebinding checks while allowing the configured TLS proxy host."""
    public = urlparse(settings.mcp_resource_url)
    authority = public.netloc.lower()
    hosts = ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*"]
    hosts.append(authority)
    if public.scheme == "https" and public.port is None:
        hosts.append(f"{authority}:443")
    return TransportSecuritySettings(
        allowed_hosts=hosts,
        allowed_origins=[
            f"{public.scheme}://{authority}",
            "https://chatgpt.com",
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        ],
    )


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
        transport_security=http_transport_security(settings),
    )


if __name__ == "__main__":
    run()

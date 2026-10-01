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
    CommandSessionInputResult,
    CommandSessionLineOutput,
    CommandSessionOutput,
    CommandSessionSnapshot,
    CommandSessionStdinCloseResult,
    DirectoryListResult,
    DirectoryTreeResult,
    FileAppendResult,
    FileEditResult,
    FileInfoResult,
    FileLineReadResult,
    FileReadManyResult,
    FileReadResult,
    FileRootListResult,
    FileSearchResult,
    FileSearchSessionListResult,
    FileSearchSessionPage,
    FileSearchSessionStopResult,
    FileWriteResult,
    GitStatusResult,
    PathMutationResult,
    PingResponse,
    PortLookupResult,
    ProcessListResult,
    ProcessSignal,
    ProcessSignalResult,
    ProcessTerminateResult,
    PtySessionDiscardResult,
    PtySessionInputResult,
    PtySessionOutput,
    PtySessionResizeResult,
    PtySessionSnapshot,
    RuntimeSessionFilter,
    ServiceAction,
    ServiceActionResult,
    ServiceLogsResult,
    ServiceStatusResult,
    SessionListResult,
    SystemHealthResult,
    TreeInspectResult,
    TreeMutationResult,
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
    async def execute(
        agent_id: str,
        argv: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> CommandResult:
        """Execute argv with optional cwd/env and a bounded one-shot timeout override."""
        return await GatewayClient(settings).execute(
            agent_id, argv, cwd=cwd, env=env, timeout_s=timeout_s
        )

    @server.tool()
    async def start_command(
        agent_id: str,
        argv: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> CommandSessionSnapshot:
        """Start a command session with optional cwd/env and a bounded timeout override."""
        return await GatewayClient(settings).start_command_session(
            agent_id, argv, cwd=cwd, env=env, timeout_s=timeout_s
        )

    @server.tool()
    async def list_sessions(
        agent_id: str,
        kind: RuntimeSessionFilter = "all",
        include_completed: bool = False,
        limit: int = 100,
    ) -> SessionListResult:
        """List active or retained command/PTY sessions without returning output contents."""
        return await GatewayClient(settings).list_sessions(
            agent_id,
            kind=kind,
            include_completed=include_completed,
            limit=limit,
        )

    @server.tool()
    async def command_status(agent_id: str, session_id: str) -> CommandSessionSnapshot:
        """Read the latest state and bounded output for a command session."""
        return await GatewayClient(settings).command_session_status(agent_id, session_id)

    @server.tool()
    async def cancel_command(agent_id: str, session_id: str) -> CommandSessionSnapshot:
        """Cancel a running command session owned by the current Agent connection."""
        return await GatewayClient(settings).cancel_command_session(agent_id, session_id)

    @server.tool()
    async def write_command_input(
        agent_id: str, session_id: str, data: str
    ) -> CommandSessionInputResult:
        """Write bounded UTF-8 stdin to a running command session."""
        return await GatewayClient(settings).write_command_input(agent_id, session_id, data)

    @server.tool()
    async def close_command_stdin(
        agent_id: str, session_id: str
    ) -> CommandSessionStdinCloseResult:
        """Close a running command session stdin to deliver EOF."""
        return await GatewayClient(settings).close_command_stdin(agent_id, session_id)

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
    async def command_output_lines(
        agent_id: str,
        session_id: str,
        stream: str = "stdout",
        offset: int = 0,
        max_lines: int = 200,
    ) -> CommandSessionLineOutput:
        """Read command output by stable line ranges or tail semantics."""
        return await GatewayClient(settings).command_session_output_lines(
            agent_id, session_id, stream=stream, offset=offset, max_lines=max_lines
        )

    @server.tool()
    async def discard_command(agent_id: str, session_id: str) -> CommandSessionDiscardResult:
        """Discard a completed command session from Agent history."""
        return await GatewayClient(settings).discard_command_session(agent_id, session_id)

    @server.tool()
    async def start_pty(
        agent_id: str,
        argv: list[str],
        approval_id: str | None = None,
        approval_secret: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        columns: int = 80,
        rows: int = 24,
    ) -> PtySessionSnapshot:
        """Start an interactive PTY. Hardened mode requires one-use approval."""
        return await GatewayClient(settings).start_pty_session(
            agent_id,
            argv,
            approval_id,
            approval_secret,
            cwd=cwd,
            env=env,
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
    async def list_directory_tree(
        agent_id: str,
        path: str,
        depth: int = 2,
        include_hidden: bool = False,
        per_directory_limit: int = 100,
        max_entries: int = 1000,
    ) -> DirectoryTreeResult:
        """List a bounded recursive directory tree without following symlinks."""
        return await GatewayClient(settings).list_directory_tree(
            agent_id, path, depth=depth, include_hidden=include_hidden,
            per_directory_limit=per_directory_limit, max_entries=max_entries,
        )

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
    async def read_file_lines(
        agent_id: str, path: str, offset: int = 0, max_lines: int = 200
    ) -> FileLineReadResult:
        """Read bounded UTF-8 text by zero-based line offset; negative offset reads a tail."""
        return await GatewayClient(settings).read_file_lines(
            agent_id, path, offset=offset, max_lines=max_lines
        )

    @server.tool()
    async def read_files(
        agent_id: str,
        paths: list[str],
        max_bytes_per_file: int = 32_768,
        max_total_bytes: int = 262_144,
    ) -> FileReadManyResult:
        """Read bounded initial chunks from multiple text files inside allowed roots."""
        return await GatewayClient(settings).read_many_files(
            agent_id,
            paths,
            max_bytes_per_file=max_bytes_per_file,
            max_total_bytes=max_total_bytes,
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
    async def append_file(
        agent_id: str,
        path: str,
        content: str,
        expected_sha256: str | None = None,
    ) -> FileAppendResult:
        """Append bounded UTF-8 text to an existing file inside allowed roots."""
        return await GatewayClient(settings).append_file(
            agent_id, path, content, expected_sha256=expected_sha256
        )

    @server.tool()
    async def edit_file(
        agent_id: str,
        path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
    ) -> FileEditResult:
        """Atomically replace exact text in one UTF-8 file inside allowed roots."""
        return await GatewayClient(settings).edit_file(
            agent_id,
            path,
            old_text,
            new_text,
            replace_all=replace_all,
        )

    @server.tool()
    async def search_files(
        agent_id: str,
        root: str,
        query: str,
        mode: str = "files",
        file_glob: str | None = None,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> FileSearchResult:
        """Search file names or UTF-8 text content recursively inside allowed roots."""
        return await GatewayClient(settings).search_files(
            agent_id,
            root,
            query,
            mode=mode,
            file_glob=file_glob,
            case_sensitive=case_sensitive,
            max_results=max_results,
        )

    @server.tool()
    async def start_search(
        agent_id: str,
        root: str,
        query: str,
        mode: str = "files",
        file_glob: str | None = None,
        case_sensitive: bool = False,
        include_hidden: bool = False,
        page_size: int = 50,
        max_results: int = 1000,
    ) -> FileSearchSessionPage:
        """Start a bounded stateful file/content search and return the first page."""
        return await GatewayClient(settings).start_search_session(
            agent_id,
            root,
            query,
            mode=mode,
            file_glob=file_glob,
            case_sensitive=case_sensitive,
            include_hidden=include_hidden,
            page_size=page_size,
            max_results=max_results,
        )

    @server.tool()
    async def list_searches(agent_id: str) -> FileSearchSessionListResult:
        """List retained stateful search sessions and their cursors."""
        return await GatewayClient(settings).list_search_sessions(agent_id)

    @server.tool()
    async def get_more_search_results(
        agent_id: str,
        session_id: str,
        offset: int | None = None,
        limit: int = 50,
    ) -> FileSearchSessionPage:
        """Read the next page or an explicit positive/negative offset range."""
        return await GatewayClient(settings).more_search_session(
            agent_id, session_id, offset=offset, limit=limit
        )

    @server.tool()
    async def stop_search(agent_id: str, session_id: str) -> FileSearchSessionStopResult:
        """Stop and discard a stateful search session."""
        return await GatewayClient(settings).stop_search_session(agent_id, session_id)

    @server.tool()
    async def inspect_tree(
        agent_id: str,
        path: str,
        max_entries: int = 5000,
        max_total_bytes: int = 268_435_456,
    ) -> TreeInspectResult:
        """Fingerprint a bounded directory tree before recursive copy/delete."""
        return await GatewayClient(settings).inspect_tree(
            agent_id,
            path,
            max_entries=max_entries,
            max_total_bytes=max_total_bytes,
        )

    @server.tool()
    async def copy_directory(
        agent_id: str,
        source: str,
        destination: str,
        expected_tree_sha256: str,
        max_entries: int = 5000,
        max_total_bytes: int = 268_435_456,
    ) -> TreeMutationResult:
        """Copy one inspected directory tree inside allowed roots."""
        return await GatewayClient(settings).mutate_tree(
            agent_id,
            "copy_tree",
            source,
            expected_tree_sha256,
            destination=destination,
            max_entries=max_entries,
            max_total_bytes=max_total_bytes,
        )

    @server.tool()
    async def delete_tree(
        agent_id: str,
        path: str,
        expected_tree_sha256: str,
        max_entries: int = 5000,
        max_total_bytes: int = 268_435_456,
    ) -> TreeMutationResult:
        """Recursively delete one inspected directory tree inside allowed roots."""
        return await GatewayClient(settings).mutate_tree(
            agent_id,
            "delete_tree",
            path,
            expected_tree_sha256,
            max_entries=max_entries,
            max_total_bytes=max_total_bytes,
        )

    @server.tool()
    async def create_directory(
        agent_id: str, path: str, parents: bool = False
    ) -> PathMutationResult:
        """Create a directory inside allowed roots."""
        return await GatewayClient(settings).mutate_path(agent_id, "mkdir", path, parents=parents)

    @server.tool()
    async def copy_file(
        agent_id: str, source: str, destination: str, overwrite: bool = False
    ) -> PathMutationResult:
        """Copy one regular file inside allowed roots."""
        return await GatewayClient(settings).mutate_path(
            agent_id, "copy", source, destination=destination, overwrite=overwrite
        )

    @server.tool()
    async def move_path(
        agent_id: str, source: str, destination: str, overwrite: bool = False
    ) -> PathMutationResult:
        """Move a non-symlink path between locations inside allowed roots."""
        return await GatewayClient(settings).mutate_path(
            agent_id, "move", source, destination=destination, overwrite=overwrite
        )

    @server.tool()
    async def delete_path(agent_id: str, path: str) -> PathMutationResult:
        """Delete one regular file or empty directory inside allowed roots; never recursive."""
        return await GatewayClient(settings).mutate_path(agent_id, "delete", path)

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
    async def signal_process(
        agent_id: str,
        pid: int,
        expected_create_time_ms: int,
        signal: ProcessSignal,
        approval_id: str | None = None,
        approval_secret: str | None = None,
    ) -> ProcessSignalResult:
        """Send TERM/KILL/INT/HUP with PID-reuse protection and mode-aware approval."""
        return await GatewayClient(settings).signal_process(
            agent_id,
            pid,
            expected_create_time_ms,
            signal,
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

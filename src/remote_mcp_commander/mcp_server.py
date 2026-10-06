from __future__ import annotations

import asyncio
import base64
import binascii
import secrets
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import httpx
from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Image
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl, Field

from remote_mcp_commander import __version__
from remote_mcp_commander.config import Settings, get_settings
from remote_mcp_commander.gateway.client import GatewayAPIError, GatewayClient
from remote_mcp_commander.oauth import OAuthTokenVerifier
from remote_mcp_commander.protocol import (
    AgentInfo,
    ArchiveCreateResult,
    ArchiveExtractResult,
    ArchiveInspectResult,
    AuditQueryResult,
    BatchCommandItemResult,
    BatchCommandResult,
    BatchCommandSpec,
    BatchDocumentPreviewItemResult,
    BatchDocumentPreviewResult,
    BatchDocumentPreviewSpec,
    BatchPathMutationItemResult,
    BatchPathMutationResult,
    BatchPathMutationSpec,
    BinaryReadResult,
    BinaryWriteResult,
    CommandDiscoveryResult,
    CommandResult,
    CommandSessionDiscardResult,
    CommandSessionInputResult,
    CommandSessionLineOutput,
    CommandSessionOutput,
    CommandSessionSnapshot,
    CommandSessionStdinCloseResult,
    DirectoryListResult,
    DirectoryTreeResult,
    DocumentEditResult,
    DocumentPreviewResult,
    DownloadChunkResult,
    DownloadStartResult,
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
    FileTailResult,
    FileWriteResult,
    GitStatusResult,
    ImagePreviewResult,
    MultiFileReadSpec,
    OperatorConfigKey,
    OperatorConfigSnapshot,
    OperatorConfigUpdateResult,
    PathMutationResult,
    PdfComposeResult,
    PdfPageSource,
    PdfRenderResult,
    PdfRewriteResult,
    PdfRewriteSegment,
    PingResponse,
    PortLookupResult,
    ProcessInfoResult,
    ProcessListResult,
    ProcessSignal,
    ProcessSignalResult,
    ProcessTerminateResult,
    PtySessionDiscardResult,
    PtySessionInputResult,
    PtySessionLineOutput,
    PtySessionOutput,
    PtySessionResizeResult,
    PtySessionSnapshot,
    RuntimeConfigResult,
    RuntimeSessionFilter,
    RuntimeSessionKind,
    ServiceAction,
    ServiceActionResult,
    ServiceLogsResult,
    ServiceStatusResult,
    SessionListResult,
    SessionSignalResult,
    SpreadsheetRow,
    SystemHealthResult,
    TransferCloseResult,
    TransferStatusResult,
    TreeInspectResult,
    TreeMutationResult,
    UploadChunkResult,
    UploadFinishResult,
    UploadStartResult,
    UsageStatsResult,
)


async def execute_many_commands(
    settings: Settings,
    agent_id: str,
    commands: list[BatchCommandSpec],
    *,
    max_concurrency: int,
) -> BatchCommandResult:
    semaphore = asyncio.Semaphore(max_concurrency)
    client = GatewayClient(settings)

    async def run_one(index: int, command: BatchCommandSpec) -> BatchCommandItemResult:
        async with semaphore:
            try:
                result = await client.execute(
                    agent_id, command.argv, cwd=command.cwd, env=command.env,
                    timeout_s=command.timeout_s,
                )
                return BatchCommandItemResult(index=index, result=result)
            except GatewayAPIError as exc:
                return BatchCommandItemResult(
                    index=index, status_code=exc.status_code, error=exc.detail
                )

    results = await asyncio.gather(
        *(run_one(index, command) for index, command in enumerate(commands))
    )
    return BatchCommandResult(results=list(results))


async def mutate_many_paths(
    settings: Settings,
    agent_id: str,
    operations: list[BatchPathMutationSpec],
    *,
    stop_on_error: bool,
) -> BatchPathMutationResult:
    client = GatewayClient(settings)
    results: list[BatchPathMutationItemResult] = []
    stopped_early = False

    for index, operation in enumerate(operations):
        failed = False
        try:
            result = await client.mutate_path(
                agent_id,
                operation.operation,
                operation.path,
                destination=operation.destination,
                parents=operation.parents,
                overwrite=operation.overwrite,
            )
            results.append(
                BatchPathMutationItemResult(
                    index=index,
                    operation=operation.operation,
                    path=operation.path,
                    result=result,
                )
            )
            failed = result.rejected
        except GatewayAPIError as exc:
            results.append(
                BatchPathMutationItemResult(
                    index=index,
                    operation=operation.operation,
                    path=operation.path,
                    status_code=exc.status_code,
                    error=exc.detail,
                )
            )
            failed = True
        except httpx.HTTPError:
            results.append(
                BatchPathMutationItemResult(
                    index=index,
                    operation=operation.operation,
                    path=operation.path,
                    status_code=503,
                    error="gateway request failed",
                )
            )
            failed = True
        except ValueError:
            results.append(
                BatchPathMutationItemResult(
                    index=index,
                    operation=operation.operation,
                    path=operation.path,
                    status_code=502,
                    error="gateway response validation failed",
                )
            )
            failed = True

        if failed and stop_on_error and index + 1 < len(operations):
            stopped_early = True
            break

    return BatchPathMutationResult(
        results=results,
        completed_count=len(results),
        stopped_early=stopped_early,
    )


async def preview_many_documents(
    settings: Settings,
    agent_id: str,
    documents: list[BatchDocumentPreviewSpec],
    *,
    max_concurrency: int,
) -> BatchDocumentPreviewResult:
    semaphore = asyncio.Semaphore(max_concurrency)
    client = GatewayClient(settings)

    async def run_one(
        index: int, document: BatchDocumentPreviewSpec
    ) -> BatchDocumentPreviewItemResult:
        async with semaphore:
            try:
                result = await client.preview_document(
                    agent_id,
                    document.path,
                    page=document.page,
                    max_pages=document.max_pages,
                    sheet=document.sheet,
                    cell_range=document.cell_range,
                    max_rows=document.max_rows,
                    max_chars=document.max_chars,
                )
                return BatchDocumentPreviewItemResult(
                    index=index, path=document.path, result=result
                )
            except GatewayAPIError as exc:
                return BatchDocumentPreviewItemResult(
                    index=index,
                    path=document.path,
                    status_code=exc.status_code,
                    error=exc.detail,
                )

    results = await asyncio.gather(
        *(run_one(index, document) for index, document in enumerate(documents))
    )
    return BatchDocumentPreviewResult(results=list(results))


_DOCUMENT_SUFFIXES = frozenset({".pdf", ".docx", ".xlsx"})
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
_MULTI_READ_MAX_IMAGE_BYTES = 2_097_152


def _image_preview_blocks(result: ImagePreviewResult) -> tuple[str, Image]:
    if result.rejected:
        raise ToolError(result.error or "image preview rejected")
    if result.format is None or result.mime_type is None or result.sha256 is None:
        raise ToolError("image preview metadata is incomplete")
    try:
        data = base64.b64decode(result.data_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ToolError("image preview payload is invalid base64") from exc
    if len(data) != result.size:
        raise ToolError("image preview payload size mismatch")
    metadata = (
        f"{result.mime_type} | {result.width}x{result.height} | "
        f"{result.size} bytes | sha256={result.sha256}"
    )
    return metadata, Image(data=data, format=result.format)


async def read_multiple_file_contents(
    settings: Settings,
    agent_id: str,
    files: list[MultiFileReadSpec],
    *,
    max_concurrency: int,
) -> list[str | Image]:
    semaphore = asyncio.Semaphore(max_concurrency)
    client = GatewayClient(settings)

    async def read_one(index: int, item: MultiFileReadSpec) -> tuple[int, str, object]:
        suffix = Path(item.path).suffix.lower()
        async with semaphore:
            try:
                if suffix in _DOCUMENT_SUFFIXES:
                    result = await client.preview_document(
                        agent_id,
                        item.path,
                        page=item.page,
                        max_pages=item.max_pages,
                        sheet=item.sheet,
                        cell_range=item.cell_range,
                        max_rows=item.max_rows,
                        max_chars=item.max_chars,
                    )
                    return index, "document", result
                if suffix in _IMAGE_SUFFIXES:
                    result = await client.preview_image(agent_id, item.path)
                    return index, "image", result
                result = await client.read_file(
                    agent_id,
                    item.path,
                    offset=0,
                    max_bytes=item.max_bytes,
                )
                return index, "text", result
            except GatewayAPIError as exc:
                return index, "error", (exc.status_code, exc.detail)
            except httpx.HTTPError:
                return index, "error", (503, "gateway request failed")
            except ValueError:
                return index, "error", (502, "gateway response validation failed")

    fetched = await asyncio.gather(
        *(read_one(index, item) for index, item in enumerate(files))
    )

    content: list[str | Image] = []
    image_bytes = 0
    for index, kind, payload in fetched:
        item = files[index]
        prefix = f"[{index}] {item.path}"
        if kind == "error":
            status_code, detail = payload
            content.append(f"{prefix} | ERROR {status_code}: {detail}")
            continue
        if kind == "text":
            result = payload
            if result.rejected:
                content.append(f"{prefix} | ERROR: {result.error or 'text read rejected'}")
                continue
            header = (
                f"{prefix} | text | {result.size} bytes | eof={result.eof} | "
                f"sha256={result.sha256 or 'unavailable'}"
            )
            content.append(f"{header}\n{result.content}")
            continue
        if kind == "document":
            result = payload
            if result.rejected:
                content.append(f"{prefix} | ERROR: {result.error or 'document preview rejected'}")
                continue
            details = [f"kind={result.kind}", f"size={result.size}"]
            if result.kind == "pdf":
                details.extend(
                    [
                        f"page={result.page}",
                        f"pages_returned={result.pages_returned}",
                        f"pages_total={result.pages_total}",
                        f"next_page={result.next_page}",
                    ]
                )
            elif result.kind == "xlsx":
                details.extend(
                    [
                        f"sheet={result.sheet}",
                        f"cell_range={result.cell_range}",
                        f"rows_returned={result.rows_returned}",
                    ]
                )
            elif result.kind == "docx":
                details.extend(
                    [
                        f"headings={len(result.headings)}",
                        f"section_breaks={result.section_breaks}",
                    ]
                )
            details.append(f"sha256={result.sha256 or 'unavailable'}")
            content.append(f"{prefix} | {' | '.join(details)}\n{result.content}")
            continue

        result = payload
        if result.rejected:
            content.append(f"{prefix} | ERROR: {result.error or 'image preview rejected'}")
            continue
        if image_bytes + result.size > _MULTI_READ_MAX_IMAGE_BYTES:
            content.append(
                f"{prefix} | ERROR: image omitted because the 2 MiB batch image budget was exceeded"
            )
            continue
        try:
            metadata, image = _image_preview_blocks(result)
        except ToolError as exc:
            content.append(f"{prefix} | ERROR: {exc}")
            continue
        image_bytes += result.size
        content.append(f"{prefix} | {metadata}")
        content.append(image)

    return content


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
    async def get_runtime_config(agent_id: str) -> RuntimeConfigResult:
        """Return sanitized runtime capabilities, roots, and command availability."""
        client = GatewayClient(settings)
        device, roots, commands = await asyncio.gather(
            client.device_info(agent_id),
            client.list_file_roots(agent_id),
            client.list_commands(agent_id),
        )
        return RuntimeConfigResult(
            mcp_version=__version__,
            agent_id=device.agent_id,
            agent_version=device.version,
            protocol_version=device.protocol_version,
            operation_mode=commands.operation_mode,
            capabilities=device.capabilities,
            allowed_roots=roots.roots,
            generic_available=commands.generic_available,
            generic_unavailable=commands.generic_unavailable,
            pty_available=commands.pty_available,
            pty_unavailable=commands.pty_unavailable,
        )

    @server.tool()
    async def get_operator_config() -> OperatorConfigSnapshot:
        """Read secret-free mutable operator limits and pending restart state."""
        return await GatewayClient(settings).get_operator_config()

    @server.tool()
    async def set_operator_config(
        key: OperatorConfigKey,
        value: int | float | None = None,
    ) -> OperatorConfigUpdateResult:
        """Persist one allowlisted Personal-mode operator limit; restart is explicit."""
        return await GatewayClient(settings).set_operator_config(key, value)

    @server.tool()
    async def get_recent_activity(
        limit: int = 50,
        event: str | None = None,
        agent_id: str | None = None,
    ) -> AuditQueryResult:
        """Return bounded, redacted recent audit activity from the Gateway."""
        return await GatewayClient(settings).list_audit_records(
            limit=limit, event=event, agent_id=agent_id
        )

    @server.tool()
    async def get_usage_stats(agent_id: str | None = None) -> UsageStatsResult:
        """Summarize bounded local audit usage without returning raw operation payloads."""
        return await GatewayClient(settings).get_usage_stats(agent_id=agent_id)

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
    async def list_commands(agent_id: str) -> CommandDiscoveryResult:
        """List command profiles that are currently resolvable on the Agent."""
        return await GatewayClient(settings).list_commands(agent_id)

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
    async def execute_many(
        agent_id: str,
        commands: Annotated[list[BatchCommandSpec], Field(min_length=1, max_length=16)],
        max_concurrency: Annotated[int, Field(ge=1, le=4)] = 4,
    ) -> BatchCommandResult:
        """Execute up to 16 independent commands with bounded concurrency."""
        return await execute_many_commands(
            settings, agent_id, commands, max_concurrency=max_concurrency
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
    async def signal_session(
        agent_id: str,
        session_id: str,
        kind: RuntimeSessionKind,
        signal: ProcessSignal,
    ) -> SessionSignalResult:
        """Send a bounded signal to a managed command or PTY session in Personal mode."""
        return await GatewayClient(settings).signal_session(
            agent_id, session_id, kind=kind, requested_signal=signal
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
        wait_ms: int = 0,
    ) -> CommandSessionLineOutput:
        """Read command output by line range/tail, optionally waiting for new stable lines."""
        return await GatewayClient(settings).command_session_output_lines(
            agent_id, session_id, stream=stream, offset=offset,
            max_lines=max_lines, wait_ms=wait_ms
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
    async def pty_output_lines(
        agent_id: str,
        session_id: str,
        offset: int = 0,
        max_lines: int = 200,
        wait_ms: int = 0,
    ) -> PtySessionLineOutput:
        """Read PTY output by line range/tail, optionally waiting for new stable lines."""
        return await GatewayClient(settings).pty_session_output_lines(
            agent_id, session_id, offset=offset, max_lines=max_lines, wait_ms=wait_ms
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
    async def preview_document(
        agent_id: str,
        path: str,
        page: int = 1,
        max_pages: int = 5,
        sheet: str | None = None,
        cell_range: str | None = None,
        max_rows: int = 200,
        max_chars: int = 65_536,
    ) -> DocumentPreviewResult:
        """Preview bounded PDF pages, DOCX structure, or an XLSX sheet/range."""
        return await GatewayClient(settings).preview_document(
            agent_id,
            path,
            page=page,
            max_pages=max_pages,
            sheet=sheet,
            cell_range=cell_range,
            max_rows=max_rows,
            max_chars=max_chars,
        )

    @server.tool()
    async def replace_docx_text(
        agent_id: str,
        path: Annotated[str, Field(min_length=1, max_length=4096)],
        old_text: Annotated[str, Field(min_length=1, max_length=8192)],
        new_text: Annotated[str, Field(max_length=8192)],
        expected_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
        expected_replacements: Annotated[int, Field(ge=1, le=100)] = 1,
    ) -> DocumentEditResult:
        """Replace exact DOCX paragraph text with SHA-guarded atomic publication."""
        return await GatewayClient(settings).replace_docx_text(
            agent_id,
            path,
            old_text,
            new_text,
            expected_sha256=expected_sha256,
            expected_replacements=expected_replacements,
        )

    @server.tool()
    async def edit_xlsx_range(
        agent_id: str,
        path: Annotated[str, Field(min_length=1, max_length=4096)],
        sheet: Annotated[str, Field(min_length=1, max_length=128)],
        cell_range: Annotated[
            str,
            Field(
                min_length=5,
                max_length=32,
                pattern=r"^[A-Za-z]{1,3}[1-9][0-9]{0,6}:[A-Za-z]{1,3}[1-9][0-9]{0,6}$",
            ),
        ],
        values: Annotated[list[SpreadsheetRow], Field(min_length=1, max_length=100)],
        expected_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
    ) -> DocumentEditResult:
        """Replace one XLSX rectangular range without evaluating formulas or macros."""
        return await GatewayClient(settings).edit_xlsx_range(
            agent_id,
            path,
            sheet,
            cell_range,
            values,
            expected_sha256=expected_sha256,
        )

    @server.tool()
    async def inspect_archive(agent_id: str, path: str) -> ArchiveInspectResult:
        """Inspect a bounded ZIP/TAR archive without extracting it."""
        return await GatewayClient(settings).inspect_archive(agent_id, path)

    @server.tool()
    async def extract_archive(
        agent_id: str,
        path: str,
        destination: str,
    ) -> ArchiveExtractResult:
        """Extract a bounded ZIP/TAR archive into a new directory."""
        return await GatewayClient(settings).extract_archive(agent_id, path, destination)

    @server.tool()
    async def create_archive(
        agent_id: str,
        source_path: str,
        output_path: str,
        expected_tree_sha256: Annotated[
            str, Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
        ],
        overwrite: bool = False,
        expected_sha256: Annotated[
            str | None,
            Field(default=None, min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$"),
        ] = None,
    ) -> ArchiveCreateResult:
        """Create a bounded ZIP/TAR archive from an inspected directory tree."""
        return await GatewayClient(settings).create_archive(
            agent_id,
            source_path,
            output_path,
            expected_tree_sha256=expected_tree_sha256,
            overwrite=overwrite,
            expected_sha256=expected_sha256,
        )

    @server.tool()
    async def compose_pdf_pages(
        agent_id: str,
        output_path: Annotated[str, Field(min_length=1, max_length=4096)],
        sources: Annotated[list[PdfPageSource], Field(min_length=1, max_length=16)],
        overwrite: bool = False,
        expected_sha256: Annotated[
            str | None,
            Field(default=None, min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$"),
        ] = None,
    ) -> PdfComposeResult:
        """Create a new PDF by selecting and concatenating bounded page ranges."""
        return await GatewayClient(settings).compose_pdf_pages(
            agent_id,
            output_path,
            sources,
            overwrite=overwrite,
            expected_sha256=expected_sha256,
        )

    @server.tool()
    async def create_pdf_from_markdown(
        agent_id: str,
        output_path: Annotated[str, Field(min_length=1, max_length=4096)],
        markdown: Annotated[str, Field(min_length=1, max_length=262_144)],
        overwrite: bool = False,
        expected_sha256: Annotated[
            str | None,
            Field(default=None, min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$"),
        ] = None,
    ) -> PdfRenderResult:
        """Create a bounded local PDF from a safe Markdown subset."""
        return await GatewayClient(settings).render_pdf_from_markdown(
            agent_id,
            output_path,
            markdown,
            overwrite=overwrite,
            expected_sha256=expected_sha256,
        )

    @server.tool()
    async def rewrite_pdf_with_markdown(
        agent_id: str,
        source_path: Annotated[str, Field(min_length=1, max_length=4096)],
        output_path: Annotated[str, Field(min_length=1, max_length=4096)],
        segments: Annotated[list[PdfRewriteSegment], Field(min_length=1, max_length=32)],
        expected_source_sha256: Annotated[
            str, Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
        ],
        overwrite: bool = False,
        expected_output_sha256: Annotated[
            str | None,
            Field(default=None, min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$"),
        ] = None,
    ) -> PdfRewriteResult:
        """Rewrite one PDF by declaring source-page and Markdown segments in final order."""
        return await GatewayClient(settings).rewrite_pdf_with_markdown(
            agent_id,
            source_path,
            output_path,
            segments,
            expected_source_sha256=expected_source_sha256,
            overwrite=overwrite,
            expected_output_sha256=expected_output_sha256,
        )

    @server.tool()
    async def preview_documents(
        agent_id: str,
        documents: Annotated[
            list[BatchDocumentPreviewSpec], Field(min_length=1, max_length=8)
        ],
        max_concurrency: Annotated[int, Field(ge=1, le=4)] = 4,
    ) -> BatchDocumentPreviewResult:
        """Preview up to 8 independent documents with bounded concurrency."""
        return await preview_many_documents(
            settings, agent_id, documents, max_concurrency=max_concurrency
        )

    @server.tool()
    async def preview_image(agent_id: str, path: str) -> list[str | Image]:
        """Render a bounded PNG, JPEG, GIF, or WebP from an Agent allowed root."""
        result = await GatewayClient(settings).preview_image(agent_id, path)
        metadata, image = _image_preview_blocks(result)
        return [metadata, image]

    @server.tool()
    async def read_multiple_files(
        agent_id: str,
        files: Annotated[list[MultiFileReadSpec], Field(min_length=1, max_length=8)],
        max_concurrency: Annotated[int, Field(ge=1, le=4)] = 4,
    ) -> list[str | Image]:
        """Read mixed text, document, and image files with automatic safe routing."""
        return await read_multiple_file_contents(
            settings,
            agent_id,
            files,
            max_concurrency=max_concurrency,
        )

    @server.tool()
    async def read_file(
        agent_id: str, path: str, offset: int = 0, max_bytes: int = 65_536
    ) -> FileReadResult:
        """Read a bounded text chunk from an absolute path inside Agent allowed roots."""
        return await GatewayClient(settings).read_file(
            agent_id, path, offset=offset, max_bytes=max_bytes
        )

    @server.tool()
    async def read_binary(
        agent_id: str,
        path: str,
        offset: Annotated[int, Field(ge=0, le=1_048_576)] = 0,
        max_bytes: Annotated[int, Field(ge=1, le=262_144)] = 262_144,
    ) -> BinaryReadResult:
        """Read a bounded binary chunk as base64 with whole-file SHA-256 metadata."""
        return await GatewayClient(settings).read_binary_file(
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
    async def tail_file(
        agent_id: str, path: str, lines: int = 100, max_bytes: int = 262_144
    ) -> FileTailResult:
        """Read the final bounded text lines from a file without scanning the whole file."""
        return await GatewayClient(settings).tail_file(
            agent_id, path, lines=lines, max_bytes=max_bytes
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
    async def write_binary(
        agent_id: str,
        path: str,
        data_base64: Annotated[str, Field(max_length=1_398_104)],
        overwrite: bool = False,
        expected_sha256: Annotated[
            str | None,
            Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
        ] = None,
    ) -> BinaryWriteResult:
        """Atomically write a bounded base64 binary payload inside Agent allowed roots."""
        return await GatewayClient(settings).write_binary_file(
            agent_id,
            path,
            data_base64,
            overwrite=overwrite,
            expected_sha256=expected_sha256,
        )

    @server.tool()
    async def start_file_upload(
        agent_id: str,
        path: str,
        size: Annotated[int, Field(ge=0, le=1_073_741_824)],
        sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        overwrite: bool = False,
        expected_sha256: Annotated[
            str | None, Field(pattern=r"^[0-9a-f]{64}$")
        ] = None,
    ) -> UploadStartResult:
        """Start a resumable bounded upload session for a large file."""
        return await GatewayClient(settings).start_upload_transfer(
            agent_id,
            path,
            size,
            sha256,
            overwrite=overwrite,
            expected_sha256=expected_sha256,
        )

    @server.tool()
    async def upload_file_chunk(
        agent_id: str,
        session_id: str,
        offset: Annotated[int, Field(ge=0, le=1_073_741_824)],
        data_base64: Annotated[str, Field(max_length=349_528)],
    ) -> UploadChunkResult:
        """Append or idempotently replay one <=256 KiB base64 upload chunk."""
        return await GatewayClient(settings).upload_transfer_chunk(
            agent_id, session_id, offset, data_base64
        )

    @server.tool()
    async def finish_file_upload(
        agent_id: str, session_id: str
    ) -> UploadFinishResult:
        """Verify full upload SHA-256 and atomically publish the completed file."""
        return await GatewayClient(settings).finish_upload_transfer(agent_id, session_id)

    @server.tool()
    async def start_file_download(
        agent_id: str, path: str
    ) -> DownloadStartResult:
        """Start a stable large-file download session and compute its SHA-256."""
        return await GatewayClient(settings).start_download_transfer(agent_id, path)

    @server.tool()
    async def download_file_chunk(
        agent_id: str,
        session_id: str,
        offset: Annotated[int, Field(ge=0, le=1_073_741_824)] = 0,
        max_bytes: Annotated[int, Field(ge=1, le=262_144)] = 262_144,
    ) -> DownloadChunkResult:
        """Read one stable <=256 KiB chunk from a large-file download session."""
        return await GatewayClient(settings).download_transfer_chunk(
            agent_id, session_id, offset=offset, max_bytes=max_bytes
        )

    @server.tool()
    async def file_transfer_status(
        agent_id: str, session_id: str
    ) -> TransferStatusResult:
        """Read progress and expiry metadata for one active file-transfer session."""
        return await GatewayClient(settings).transfer_status(agent_id, session_id)

    @server.tool()
    async def close_file_transfer(
        agent_id: str, session_id: str
    ) -> TransferCloseResult:
        """Close a transfer; unfinished uploads are discarded and downloads are released."""
        return await GatewayClient(settings).close_transfer(agent_id, session_id)

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
    async def mutate_paths(
        agent_id: str,
        operations: Annotated[
            list[BatchPathMutationSpec], Field(min_length=1, max_length=16)
        ],
        stop_on_error: bool = True,
    ) -> BatchPathMutationResult:
        """Run up to 16 filesystem mutations sequentially in input order."""
        return await mutate_many_paths(
            settings,
            agent_id,
            operations,
            stop_on_error=stop_on_error,
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
    async def process_info(agent_id: str, pid: int) -> ProcessInfoResult:
        """Read metadata for one PID without exposing command-line arguments or environments."""
        return await GatewayClient(settings).process_info(agent_id, pid)

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

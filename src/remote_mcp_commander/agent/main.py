from __future__ import annotations

import asyncio
import json
import platform
import socket

import websockets

from remote_mcp_commander import __version__
from remote_mcp_commander.agent.capabilities import detect_capabilities
from remote_mcp_commander.agent.diagnostics import lookup_port, service_logs, system_health
from remote_mcp_commander.agent.edit_ops import edit_text_file
from remote_mcp_commander.agent.file_ops import (
    allowed_roots,
    read_many_text_files,
    read_text_file,
    write_text_file,
)
from remote_mcp_commander.agent.filesystem_ops import (
    file_info,
    list_directory,
    list_directory_tree,
    list_file_roots,
)
from remote_mcp_commander.agent.git_ops import git_status
from remote_mcp_commander.agent.one_shot import OneShotCommandDispatcher
from remote_mcp_commander.agent.path_ops import mutate_path
from remote_mcp_commander.agent.pty_ops import PtySessionManager
from remote_mcp_commander.agent.search_ops import search_files
from remote_mcp_commander.agent.search_session_ops import (
    FileSearchSessionManager,
    FileSearchStartDispatcher,
)
from remote_mcp_commander.agent.session_ops import CommandSessionManager
from remote_mcp_commander.agent.system_ops import (
    list_processes,
    service_action,
    service_status,
    signal_process,
    terminate_process,
)
from remote_mcp_commander.agent.tree_ops import inspect_tree, mutate_tree
from remote_mcp_commander.config import get_settings
from remote_mcp_commander.protocol import (
    PROTOCOL_MAX_SUPPORTED,
    PROTOCOL_MIN_SUPPORTED,
    AgentHello,
    CommandRequest,
    CommandSessionCancelRequest,
    CommandSessionDiscardRequest,
    CommandSessionOutputRequest,
    CommandSessionStartRequest,
    CommandSessionStatusRequest,
    DirectoryListRequest,
    DirectoryTreeRequest,
    FileEditRequest,
    FileInfoRequest,
    FileReadManyRequest,
    FileReadRequest,
    FileRootListRequest,
    FileSearchRequest,
    FileSearchSessionListRequest,
    FileSearchSessionMoreRequest,
    FileSearchSessionStartRequest,
    FileSearchSessionStopRequest,
    FileWriteRequest,
    GitStatusRequest,
    Heartbeat,
    PathMutationRequest,
    PingRequest,
    PingResult,
    PortLookupRequest,
    ProcessListRequest,
    ProcessSignalRequest,
    ProcessTerminateRequest,
    PtySessionCancelRequest,
    PtySessionDiscardRequest,
    PtySessionInputRequest,
    PtySessionOutputRequest,
    PtySessionResizeRequest,
    PtySessionStartRequest,
    PtySessionStatusRequest,
    ServiceActionRequest,
    ServiceLogsRequest,
    ServiceStatusRequest,
    SessionListRequest,
    SessionListResult,
    SystemHealthRequest,
    TreeInspectRequest,
    TreeMutationRequest,
)


async def heartbeat_loop(websocket: websockets.ClientConnection, agent_id: str) -> None:
    while True:
        await asyncio.sleep(15)
        await websocket.send(Heartbeat(agent_id=agent_id).model_dump_json())


async def agent_loop() -> None:
    settings = get_settings()
    token = settings.load_agent_token()
    settings.validate_agent_security(token)
    headers = {"Authorization": f"Bearer {token}"}
    roots = allowed_roots(settings.allowed_roots)

    while True:
        try:
            async with websockets.connect(
                settings.gateway_ws,
                additional_headers=headers,
                ping_interval=20,
                ping_timeout=20,
                max_size=2_097_152,
            ) as websocket:
                sessions = CommandSessionManager(
                    allowlist=settings.executable_allowlist,
                    timeout_s=settings.session_timeout_s,
                    max_output_bytes=settings.max_output_bytes,
                    max_active=settings.session_max_active,
                    history_limit=settings.session_history_limit,
                    exec_search_path=settings.command_search_path,
                    policy_mode=settings.operation_mode,
                    child_env=settings.command_environment,
                    roots=roots,
                )
                pty_sessions = PtySessionManager(
                    allowlist=settings.pty_executable_allowlist,
                    timeout_s=settings.pty_timeout_s,
                    max_output_bytes=settings.max_output_bytes,
                    max_input_bytes=settings.pty_input_max_bytes,
                    max_active=settings.pty_max_active,
                    history_limit=settings.session_history_limit,
                    exec_search_path=settings.command_search_path,
                    child_env=settings.command_environment,
                    personal_mode=settings.personal_mode,
                    roots=roots,
                )
                one_shot = OneShotCommandDispatcher(
                    allowlist=settings.executable_allowlist,
                    timeout_s=settings.exec_timeout_s,
                    max_output_bytes=settings.max_output_bytes,
                    max_active=settings.session_max_active,
                    exec_search_path=settings.command_search_path,
                    policy_mode=settings.operation_mode,
                    child_env=settings.command_environment,
                    roots=roots,
                )
                search_sessions = FileSearchSessionManager(roots=roots)
                search_starts = FileSearchStartDispatcher(search_sessions)
                hello = AgentHello(
                    agent_id=settings.agent_id,
                    hostname=socket.gethostname(),
                    platform=platform.platform(),
                    version=__version__,
                    protocol_min=PROTOCOL_MIN_SUPPORTED,
                    protocol_max=PROTOCOL_MAX_SUPPORTED,
                    capabilities=detect_capabilities(settings, roots),
                )
                await websocket.send(hello.model_dump_json())
                heartbeat_task = asyncio.create_task(heartbeat_loop(websocket, settings.agent_id))
                try:
                    async for raw in websocket:
                        payload = json.loads(raw)
                        message_type = payload.get("type")
                        if message_type == "ping_request":
                            ping = PingRequest.model_validate(payload)
                            await websocket.send(
                                PingResult(request_id=ping.request_id).model_dump_json()
                            )
                            continue

                        if message_type == "system_health_request":
                            request = SystemHealthRequest.model_validate(payload)
                            result = await system_health(request.request_id)
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "port_lookup_request":
                            request = PortLookupRequest.model_validate(payload)
                            result = await lookup_port(request.request_id, request.port)
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "service_logs_request":
                            request = ServiceLogsRequest.model_validate(payload)
                            result = await service_logs(
                                request.request_id,
                                request.unit,
                                request.lines,
                                timeout_s=settings.exec_timeout_s,
                                max_output_bytes=settings.max_output_bytes,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "git_status_request":
                            request = GitStatusRequest.model_validate(payload)
                            result = await git_status(
                                request.request_id,
                                request.path,
                                roots=roots,
                                timeout_s=settings.exec_timeout_s,
                                max_output_bytes=settings.max_output_bytes,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "session_list_request":
                            request = SessionListRequest.model_validate(payload)
                            infos = []
                            if request.kind in {"all", "command"}:
                                infos.extend(
                                    await sessions.list_infos(
                                        include_completed=request.include_completed
                                    )
                                )
                            if request.kind in {"all", "pty"}:
                                infos.extend(
                                    await pty_sessions.list_infos(
                                        include_completed=request.include_completed
                                    )
                                )
                            infos.sort(
                                key=lambda info: (
                                    info.started_at.timestamp() if info.started_at else 0.0
                                ),
                                reverse=True,
                            )
                            total_count = len(infos)
                            result = SessionListResult(
                                request_id=request.request_id,
                                sessions=infos[: request.limit],
                                total_count=total_count,
                                truncated=total_count > request.limit,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "command_session_start_request":
                            request = CommandSessionStartRequest.model_validate(payload)
                            result = await sessions.start(
                                request.request_id,
                                request.session_id,
                                request.argv,
                                cwd=request.cwd,
                                env_overrides=request.env,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "command_session_status_request":
                            request = CommandSessionStatusRequest.model_validate(payload)
                            result = await sessions.status(request.request_id, request.session_id)
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "command_session_cancel_request":
                            request = CommandSessionCancelRequest.model_validate(payload)
                            result = await sessions.cancel(request.request_id, request.session_id)
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "command_session_output_request":
                            request = CommandSessionOutputRequest.model_validate(payload)
                            result = await sessions.output(
                                request.request_id,
                                request.session_id,
                                stdout_offset=request.stdout_offset,
                                stderr_offset=request.stderr_offset,
                                max_chars=request.max_chars,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "command_session_discard_request":
                            request = CommandSessionDiscardRequest.model_validate(payload)
                            result = await sessions.discard(request.request_id, request.session_id)
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "pty_session_start_request":
                            request = PtySessionStartRequest.model_validate(payload)
                            result = await pty_sessions.start(
                                request.request_id,
                                request.session_id,
                                request.argv,
                                cwd=request.cwd,
                                env_overrides=request.env,
                                columns=request.columns,
                                rows=request.rows,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "pty_session_status_request":
                            request = PtySessionStatusRequest.model_validate(payload)
                            result = await pty_sessions.status(
                                request.request_id, request.session_id
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "pty_session_input_request":
                            request = PtySessionInputRequest.model_validate(payload)
                            result = await pty_sessions.input(
                                request.request_id, request.session_id, request.data
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "pty_session_resize_request":
                            request = PtySessionResizeRequest.model_validate(payload)
                            result = await pty_sessions.resize(
                                request.request_id,
                                request.session_id,
                                columns=request.columns,
                                rows=request.rows,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "pty_session_cancel_request":
                            request = PtySessionCancelRequest.model_validate(payload)
                            result = await pty_sessions.cancel(
                                request.request_id, request.session_id
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "pty_session_output_request":
                            request = PtySessionOutputRequest.model_validate(payload)
                            result = await pty_sessions.output(
                                request.request_id,
                                request.session_id,
                                offset=request.offset,
                                max_chars=request.max_chars,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "pty_session_discard_request":
                            request = PtySessionDiscardRequest.model_validate(payload)
                            result = await pty_sessions.discard(
                                request.request_id, request.session_id
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "file_root_list_request":
                            request = FileRootListRequest.model_validate(payload)
                            result = await list_file_roots(request.request_id, roots)
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "directory_list_request":
                            request = DirectoryListRequest.model_validate(payload)
                            result = await list_directory(
                                request.request_id,
                                request.path,
                                roots=roots,
                                limit=request.limit,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "directory_tree_request":
                            request = DirectoryTreeRequest.model_validate(payload)
                            result = await list_directory_tree(
                                request.request_id,
                                request.path,
                                roots=roots,
                                depth=request.depth,
                                include_hidden=request.include_hidden,
                                per_directory_limit=request.per_directory_limit,
                                max_entries=request.max_entries,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "file_info_request":
                            request = FileInfoRequest.model_validate(payload)
                            result = await file_info(
                                request.request_id,
                                request.path,
                                roots=roots,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "file_read_request":
                            request = FileReadRequest.model_validate(payload)
                            result = await read_text_file(
                                request.request_id,
                                request.path,
                                roots=roots,
                                offset=request.offset,
                                max_bytes=request.max_bytes,
                                max_file_bytes=settings.file_max_bytes,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "file_read_many_request":
                            request = FileReadManyRequest.model_validate(payload)
                            result = await read_many_text_files(
                                request.request_id,
                                request.paths,
                                roots=roots,
                                max_bytes_per_file=request.max_bytes_per_file,
                                max_total_bytes=request.max_total_bytes,
                                max_file_bytes=settings.file_max_bytes,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "file_write_request":
                            request = FileWriteRequest.model_validate(payload)
                            result = await write_text_file(
                                request.request_id,
                                request.path,
                                request.content,
                                roots=roots,
                                overwrite=request.overwrite,
                                expected_sha256=request.expected_sha256,
                                max_file_bytes=settings.file_max_bytes,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "file_edit_request":
                            request = FileEditRequest.model_validate(payload)
                            result = await edit_text_file(
                                request.request_id,
                                request.path,
                                request.old_text,
                                request.new_text,
                                roots=roots,
                                replace_all=request.replace_all,
                                max_file_bytes=settings.file_max_bytes,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "file_search_request":
                            request = FileSearchRequest.model_validate(payload)
                            result = await search_files(
                                request.request_id,
                                request.root,
                                request.query,
                                roots=roots,
                                mode=request.mode,
                                file_glob=request.file_glob,
                                case_sensitive=request.case_sensitive,
                                max_results=request.max_results,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "file_search_session_start_request":
                            request = FileSearchSessionStartRequest.model_validate(payload)
                            await search_starts.submit(request, websocket.send)
                            continue

                        if message_type == "file_search_session_more_request":
                            request = FileSearchSessionMoreRequest.model_validate(payload)
                            result = await search_sessions.more(
                                request.request_id,
                                request.session_id,
                                request.limit,
                                offset=request.offset,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "file_search_session_list_request":
                            request = FileSearchSessionListRequest.model_validate(payload)
                            result = await search_sessions.list_sessions(request.request_id)
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "file_search_session_stop_request":
                            request = FileSearchSessionStopRequest.model_validate(payload)
                            result = await search_sessions.stop(
                                request.request_id, request.session_id
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "path_mutation_request":
                            request = PathMutationRequest.model_validate(payload)
                            result = await mutate_path(
                                request.request_id,
                                request.operation,
                                request.path,
                                roots=roots,
                                destination=request.destination,
                                parents=request.parents,
                                overwrite=request.overwrite,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "tree_inspect_request":
                            request = TreeInspectRequest.model_validate(payload)
                            result = await inspect_tree(
                                request.request_id,
                                request.path,
                                roots=roots,
                                max_entries=request.max_entries,
                                max_total_bytes=request.max_total_bytes,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "tree_mutation_request":
                            request = TreeMutationRequest.model_validate(payload)
                            result = await mutate_tree(
                                request.request_id,
                                request.operation,
                                request.path,
                                roots=roots,
                                destination=request.destination,
                                expected_tree_sha256=request.expected_tree_sha256,
                                max_entries=request.max_entries,
                                max_total_bytes=request.max_total_bytes,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "process_list_request":
                            request = ProcessListRequest.model_validate(payload)
                            result = await list_processes(request.request_id, request.limit)
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "service_status_request":
                            request = ServiceStatusRequest.model_validate(payload)
                            result = await service_status(
                                request.request_id, request.unit, settings.exec_timeout_s
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "process_terminate_request":
                            request = ProcessTerminateRequest.model_validate(payload)
                            result = await terminate_process(
                                request.request_id,
                                request.pid,
                                request.expected_create_time_ms,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "process_signal_request":
                            request = ProcessSignalRequest.model_validate(payload)
                            result = await signal_process(
                                request.request_id,
                                request.pid,
                                request.expected_create_time_ms,
                                request.signal,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type == "service_action_request":
                            request = ServiceActionRequest.model_validate(payload)
                            result = await service_action(
                                request.request_id,
                                request.unit,
                                request.action,
                                settings.exec_timeout_s,
                            )
                            await websocket.send(result.model_dump_json())
                            continue

                        if message_type != "command_request":
                            continue
                        request = CommandRequest.model_validate(payload)
                        await one_shot.submit(request, websocket.send)
                finally:
                    heartbeat_task.cancel()
                    await asyncio.gather(
                        sessions.cancel_all(),
                        pty_sessions.cancel_all(),
                        one_shot.cancel_all(),
                        search_starts.cancel_all(),
                    )
                    await asyncio.gather(heartbeat_task, return_exceptions=True)
        except (OSError, websockets.ConnectionClosed):
            await asyncio.sleep(2)


def run() -> None:
    asyncio.run(agent_loop())


if __name__ == "__main__":
    run()

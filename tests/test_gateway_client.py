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


@pytest.mark.asyncio
async def test_file_methods_send_structured_payloads() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/files/read"):
            return httpx.Response(
                200,
                json={
                    "request_id": "read-1",
                    "path": "/srv/project/a.txt",
                    "content": "abc",
                    "size": 3,
                    "offset": 0,
                    "next_offset": 3,
                    "eof": True,
                    "sha256": "0" * 64,
                },
            )
        return httpx.Response(
            200,
            json={
                "request_id": "write-1",
                "path": "/srv/project/a.txt",
                "bytes_written": 3,
                "sha256": "1" * 64,
            },
        )

    client = GatewayClient(make_settings(), transport=httpx.MockTransport(handler))
    read_result = await client.read_file("server-01", "/srv/project/a.txt", offset=0, max_bytes=123)
    write_result = await client.write_file(
        "server-01",
        "/srv/project/a.txt",
        "xyz",
        overwrite=True,
        expected_sha256="0" * 64,
    )

    assert read_result.content == "abc"
    assert write_result.bytes_written == 3
    assert len(requests) == 2
    assert requests[0].url.path.endswith("/files/read")
    assert requests[1].url.path.endswith("/files/write")
    read_payload = requests[0].content.decode("utf-8")
    write_payload = requests[1].content.decode("utf-8")
    assert '"max_bytes":123' in read_payload
    assert '"content":"xyz"' in write_payload
    assert '"overwrite":true' in write_payload
    assert '"expected_sha256":"' + ("0" * 64) + '"' in write_payload


@pytest.mark.asyncio
async def test_mutation_methods_send_approval_binding() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/processes/terminate"):
            return httpx.Response(
                200,
                json={
                    "request_id": "term-1",
                    "pid": 123,
                    "signal_sent": True,
                    "exited": True,
                },
            )
        return httpx.Response(
            200,
            json={
                "request_id": "svc-1",
                "unit": "demo.service",
                "action": "restart",
                "returncode": 0,
            },
        )

    client = GatewayClient(make_settings(), transport=httpx.MockTransport(handler))
    await client.terminate_process(
        "server-01",
        123,
        456789,
        "approval-1",
        "approval-secret-value",
    )
    await client.service_action(
        "server-01",
        "demo.service",
        "restart",
        "approval-2",
        "approval-secret-value",
    )

    terminate_payload = requests[0].content.decode()
    service_payload = requests[1].content.decode()
    assert requests[0].url.path.endswith("/processes/terminate")
    assert '"expected_create_time_ms":456789' in terminate_payload
    assert '"approval_id":"approval-1"' in terminate_payload
    assert requests[1].url.path.endswith("/services/action")
    assert '"action":"restart"' in service_payload
    assert '"approval_id":"approval-2"' in service_payload


@pytest.mark.asyncio
async def test_command_session_methods_use_expected_routes() -> None:
    requests: list[httpx.Request] = []
    session_id = "a" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/output"):
            return httpx.Response(
                200,
                json={
                    "request_id": "output-request",
                    "session_id": session_id,
                    "state": "completed",
                    "stdout": "new",
                    "next_stdout_offset": 7,
                    "next_stderr_offset": 0,
                },
            )
        if request.url.path.endswith("/discard"):
            return httpx.Response(
                200,
                json={
                    "request_id": "discard-request",
                    "session_id": session_id,
                    "discarded": True,
                },
            )
        if request.method == "POST" and request.url.path.endswith("/commands/sessions"):
            state = "running"
        elif request.method == "POST" and request.url.path.endswith("/cancel"):
            state = "cancelled"
        else:
            state = "completed"
        return httpx.Response(
            200,
            json={
                "request_id": "session-request",
                "session_id": session_id,
                "executable": "uptime",
                "state": state,
            },
        )

    client = GatewayClient(make_settings(), transport=httpx.MockTransport(handler))
    started = await client.start_command_session("server-01", ["uptime"])
    status = await client.command_session_status("server-01", session_id)
    cancelled = await client.cancel_command_session("server-01", session_id)
    output = await client.command_session_output(
        "server-01", session_id, stdout_offset=3, stderr_offset=0, max_chars=4
    )
    discarded = await client.discard_command_session("server-01", session_id)

    assert started.state == "running"
    assert status.state == "completed"
    assert cancelled.state == "cancelled"
    assert output.stdout == "new"
    assert output.next_stdout_offset == 7
    assert discarded.discarded is True
    assert requests[0].url.path.endswith("/commands/sessions")
    assert requests[1].url.path.endswith(f"/commands/sessions/{session_id}")
    assert requests[2].url.path.endswith(f"/commands/sessions/{session_id}/cancel")
    assert requests[3].url.path.endswith(f"/commands/sessions/{session_id}/output")
    assert requests[4].url.path.endswith(f"/commands/sessions/{session_id}/discard")
    assert b'"argv":["uptime"]' in requests[0].content
    assert b'"stdout_offset":3' in requests[3].content
    assert b'"max_chars":4' in requests[3].content


@pytest.mark.asyncio
async def test_pty_session_methods_use_expected_routes_and_payloads() -> None:
    requests: list[httpx.Request] = []
    session_id = "b" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/input"):
            return httpx.Response(
                200,
                json={
                    "request_id": "input",
                    "session_id": session_id,
                    "accepted_bytes": 2,
                },
            )
        if path.endswith("/resize"):
            return httpx.Response(
                200,
                json={
                    "request_id": "resize",
                    "session_id": session_id,
                    "columns": 120,
                    "rows": 40,
                },
            )
        if path.endswith("/output"):
            return httpx.Response(
                200,
                json={
                    "request_id": "output",
                    "session_id": session_id,
                    "state": "running",
                    "output": "ok",
                    "next_offset": 5,
                },
            )
        if path.endswith("/discard"):
            return httpx.Response(
                200,
                json={
                    "request_id": "discard",
                    "session_id": session_id,
                    "discarded": True,
                },
            )
        state = "cancelled" if path.endswith("/cancel") else "running"
        return httpx.Response(
            200,
            json={
                "request_id": "snapshot",
                "session_id": session_id,
                "executable": "bash",
                "state": state,
            },
        )

    client = GatewayClient(make_settings(), transport=httpx.MockTransport(handler))
    started = await client.start_pty_session(
        "server-01",
        ["bash"],
        "approval-1",
        "approval-secret-value",
        columns=100,
        rows=30,
    )
    status = await client.pty_session_status("server-01", session_id)
    accepted = await client.write_pty_input("server-01", session_id, "x\n")
    resized = await client.resize_pty_session("server-01", session_id, columns=120, rows=40)
    output = await client.pty_session_output("server-01", session_id, offset=3, max_chars=4)
    cancelled = await client.cancel_pty_session("server-01", session_id)
    discarded = await client.discard_pty_session("server-01", session_id)

    assert started.state == "running"
    assert status.state == "running"
    assert accepted.accepted_bytes == 2
    assert (resized.columns, resized.rows) == (120, 40)
    assert output.output == "ok"
    assert cancelled.state == "cancelled"
    assert discarded.discarded is True
    assert requests[0].url.path.endswith("/pty/sessions")
    assert b'"argv":["bash"]' in requests[0].content
    assert b'"approval_id":"approval-1"' in requests[0].content
    assert requests[1].method == "GET"
    assert requests[2].url.path.endswith("/input")
    assert requests[3].url.path.endswith("/resize")
    assert requests[4].url.path.endswith("/output")
    assert requests[5].url.path.endswith("/cancel")
    assert requests[6].url.path.endswith("/discard")


@pytest.mark.asyncio
async def test_diagnostic_methods_use_expected_routes_and_payloads() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/health"):
            return httpx.Response(200, json={"request_id": "health", "uptime_seconds": 10})
        if path.endswith("/port"):
            return httpx.Response(200, json={"request_id": "port", "port": 8005})
        if path.endswith("/service-logs"):
            return httpx.Response(
                200,
                json={"request_id": "logs", "unit": "demo.service", "text": "ok"},
            )
        return httpx.Response(
            200,
            json={
                "request_id": "git",
                "path": "/srv/repo",
                "repo_root": "/srv/repo",
                "clean": True,
            },
        )

    client = GatewayClient(make_settings(), transport=httpx.MockTransport(handler))
    health = await client.system_health("server-01")
    port = await client.lookup_port("server-01", 8005)
    logs = await client.service_logs("server-01", "demo.service", lines=25)
    git = await client.git_status("server-01", "/srv/repo")

    assert health.uptime_seconds == 10
    assert port.port == 8005
    assert logs.text == "ok"
    assert git.clean is True
    assert requests[0].url.path.endswith("/diagnostics/health")
    assert requests[1].url.path.endswith("/diagnostics/port")
    assert requests[2].url.path.endswith("/diagnostics/service-logs")
    assert requests[3].url.path.endswith("/diagnostics/git-status")
    assert b'"port":8005' in requests[1].content
    assert b'"unit":"demo.service"' in requests[2].content
    assert b'"lines":25' in requests[2].content
    assert b'"path":"/srv/repo"' in requests[3].content


@pytest.mark.asyncio
async def test_filesystem_discovery_methods_use_expected_routes() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/roots"):
            return httpx.Response(
                200,
                json={"request_id": "roots", "roots": ["/srv/project"]},
            )
        if request.url.path.endswith("/list"):
            return httpx.Response(
                200,
                json={
                    "request_id": "list",
                    "path": "/srv/project",
                    "entries": [],
                },
            )
        return httpx.Response(
            200,
            json={
                "request_id": "info",
                "path": "/srv/project/file.txt",
                "kind": "file",
                "size": 3,
            },
        )

    client = GatewayClient(make_settings(), transport=httpx.MockTransport(handler))
    roots = await client.list_file_roots("server-01")
    listed = await client.list_directory("server-01", "/srv/project", limit=25)
    info = await client.file_info("server-01", "/srv/project/file.txt")

    assert roots.roots == ["/srv/project"]
    assert listed.path == "/srv/project"
    assert info.kind == "file"
    assert requests[0].method == "GET"
    assert requests[0].url.path.endswith("/files/roots")
    assert requests[1].method == "POST"
    assert requests[1].url.path.endswith("/files/list")
    assert b'"path":"/srv/project"' in requests[1].content
    assert b'"limit":25' in requests[1].content
    assert requests[2].url.path.endswith("/files/info")
    assert b'"path":"/srv/project/file.txt"' in requests[2].content


@pytest.mark.asyncio
async def test_search_and_path_mutation_methods_use_expected_routes() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/files/search"):
            return httpx.Response(200, json={"request_id": "s", "matches": [], "scanned_files": 1})
        return httpx.Response(
            200,
            json={
                "request_id": "m",
                "operation": request.url.path.rsplit("/", 1)[-1],
                "path": "/home/ubuntu/a",
                "changed": True,
            },
        )

    client = GatewayClient(make_settings(), transport=httpx.MockTransport(handler))
    await client.search_files(
        "server-01", "/home/ubuntu", "needle", mode="content", file_glob="*.py"
    )
    await client.mutate_path("server-01", "mkdir", "/home/ubuntu/a", parents=True)
    await client.mutate_path(
        "server-01",
        "copy",
        "/home/ubuntu/a.txt",
        destination="/home/ubuntu/b.txt",
        overwrite=True,
    )

    assert requests[0].url.path.endswith("/files/search")
    assert '"mode":"content"' in requests[0].content.decode()
    assert '"file_glob":"*.py"' in requests[0].content.decode()
    assert requests[1].url.path.endswith("/files/mkdir")
    assert '"parents":true' in requests[1].content.decode()
    assert requests[2].url.path.endswith("/files/copy")
    assert '"destination":"/home/ubuntu/b.txt"' in requests[2].content.decode()


@pytest.mark.asyncio
async def test_edit_file_sends_exact_replacement_payload() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "request_id": "edit-1",
                "path": "/home/ubuntu/demo.txt",
                "replacements": 1,
                "bytes_written": 5,
                "sha256": "2" * 64,
            },
        )

    client = GatewayClient(make_settings(), transport=httpx.MockTransport(handler))
    result = await client.edit_file(
        "server-01",
        "/home/ubuntu/demo.txt",
        "old",
        "new",
        replace_all=True,
    )
    assert result.replacements == 1
    assert seen[0].url.path.endswith("/files/edit")
    payload = seen[0].content.decode()
    assert '"old_text":"old"' in payload
    assert '"new_text":"new"' in payload
    assert '"replace_all":true' in payload


@pytest.mark.asyncio
async def test_command_clients_send_optional_working_directory() -> None:
    requests: list[httpx.Request] = []
    session_id = "c" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/execute"):
            return httpx.Response(200, json={"request_id": "e", "returncode": 0})
        return httpx.Response(
            200,
            json={
                "request_id": "s",
                "session_id": session_id,
                "state": "running",
            },
        )

    client = GatewayClient(make_settings(), transport=httpx.MockTransport(handler))
    cwd = "/srv/project"
    await client.execute("server-01", ["pwd"], cwd=cwd)
    await client.start_command_session("server-01", ["pwd"], cwd=cwd)

    assert len(requests) == 2
    for request in requests:
        assert b'"cwd":"/srv/project"' in request.content


@pytest.mark.asyncio
async def test_read_many_files_sends_bounded_payload() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "request_id": "batch",
                "files": [],
                "requested_count": 2,
                "total_bytes": 0,
                "truncated": False,
            },
        )

    client = GatewayClient(make_settings(), transport=httpx.MockTransport(handler))
    result = await client.read_many_files(
        "server-01",
        ["/srv/a.py", "/srv/b.py"],
        max_bytes_per_file=1234,
        max_total_bytes=4321,
    )

    assert result.requested_count == 2
    assert requests[0].url.path.endswith("/files/read-many")
    payload = requests[0].content
    assert b'"max_bytes_per_file":1234' in payload
    assert b'"max_total_bytes":4321' in payload


@pytest.mark.asyncio
async def test_stateful_search_client_uses_session_routes() -> None:
    requests: list[httpx.Request] = []
    session_id = "e" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/files/search/sessions"):
            return httpx.Response(200, json={"request_id": "list", "sessions": []})
        if request.url.path.endswith("/stop"):
            return httpx.Response(
                200,
                json={
                    "request_id": "stop",
                    "session_id": session_id,
                    "stopped": True,
                },
            )
        return httpx.Response(
            200,
            json={
                "request_id": "page",
                "session_id": session_id,
                "matches": [],
                "returned_count": 0,
                "remaining": 0,
                "exhausted": True,
            },
        )

    client = GatewayClient(make_settings(), transport=httpx.MockTransport(handler))
    await client.start_search_session(
        "server-01",
        "/srv/project",
        "needle",
        mode="content",
        page_size=25,
        max_results=500,
    )
    await client.list_search_sessions("server-01")
    await client.more_search_session("server-01", session_id, offset=-20, limit=30)
    await client.stop_search_session("server-01", session_id)

    assert requests[0].url.path.endswith("/files/search/sessions")
    assert b'"page_size":25' in requests[0].content
    assert b'"max_results":500' in requests[0].content
    assert requests[1].method == "GET"
    assert requests[1].url.path.endswith("/files/search/sessions")
    assert requests[2].url.path.endswith(f"/{session_id}/more")
    assert b'"offset":-20' in requests[2].content
    assert b'"limit":30' in requests[2].content
    assert requests[3].url.path.endswith(f"/{session_id}/stop")


@pytest.mark.asyncio
async def test_tree_client_uses_inspect_copy_and_delete_routes() -> None:
    requests: list[httpx.Request] = []
    tree_hash = "a" * 64

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/inspect"):
            return httpx.Response(
                200,
                json={
                    "request_id": "inspect",
                    "path": "/srv/tree",
                    "entries": 2,
                    "total_bytes": 10,
                    "tree_sha256": tree_hash,
                },
            )
        return httpx.Response(
            200,
            json={
                "request_id": "mutation",
                "operation": "copy_tree" if request.url.path.endswith("/copy") else "delete_tree",
                "path": "/srv/tree",
                "changed": True,
            },
        )

    client = GatewayClient(make_settings(), transport=httpx.MockTransport(handler))
    await client.inspect_tree("server-01", "/srv/tree", max_entries=100, max_total_bytes=1024)
    await client.mutate_tree(
        "server-01",
        "copy_tree",
        "/srv/tree",
        tree_hash,
        destination="/srv/tree-copy",
        max_entries=100,
        max_total_bytes=1024,
    )
    await client.mutate_tree(
        "server-01",
        "delete_tree",
        "/srv/tree",
        tree_hash,
        max_entries=100,
        max_total_bytes=1024,
    )

    assert requests[0].url.path.endswith("/files/tree/inspect")
    assert requests[1].url.path.endswith("/files/tree/copy")
    assert b'"destination":"/srv/tree-copy"' in requests[1].content
    assert requests[2].url.path.endswith("/files/tree/delete")
    assert tree_hash.encode() in requests[2].content


@pytest.mark.asyncio
async def test_signal_process_client_sends_structured_signal_payload() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "request_id": "sig",
                "pid": 123,
                "signal": "kill",
                "signal_sent": True,
                "exited": True,
            },
        )

    client = GatewayClient(make_settings(), transport=httpx.MockTransport(handler))
    result = await client.signal_process("server-01", 123, 456789, "kill")
    assert result.signal == "kill"
    assert result.signal_sent is True
    assert seen[0].url.path.endswith("/processes/signal")
    payload = seen[0].content.decode()
    assert '"pid":123' in payload
    assert '"expected_create_time_ms":456789' in payload
    assert '"signal":"kill"' in payload


@pytest.mark.asyncio
async def test_command_clients_send_structured_env_overrides() -> None:
    requests: list[httpx.Request] = []
    session_id = "9" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/execute"):
            return httpx.Response(200, json={"request_id": "e", "returncode": 0})
        if request.url.path.endswith("/commands/sessions"):
            return httpx.Response(
                200,
                json={"request_id": "s", "session_id": session_id, "state": "running"},
            )
        return httpx.Response(
            200,
            json={
                "request_id": "p",
                "session_id": session_id,
                "executable": "bash",
                "state": "running",
                "columns": 80,
                "rows": 24,
            },
        )

    client = GatewayClient(make_settings(), transport=httpx.MockTransport(handler))
    env = {"DEMO_FLAG": "structured-value"}
    await client.execute("server-01", ["echo", "ok"], env=env)
    await client.start_command_session("server-01", ["echo", "ok"], env=env)
    await client.start_pty_session("server-01", ["bash"], env=env)

    assert len(requests) == 3
    for request in requests:
        assert b'"env":{"DEMO_FLAG":"structured-value"}' in request.content


@pytest.mark.asyncio
async def test_runtime_session_list_client_sends_filters() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "request_id": "sessions",
                "sessions": [],
                "total_count": 0,
                "truncated": False,
            },
        )

    client = GatewayClient(make_settings(), transport=httpx.MockTransport(handler))
    result = await client.list_sessions(
        "server-01", kind="pty", include_completed=True, limit=25
    )
    assert result.total_count == 0
    assert seen[0].url.path.endswith("/sessions")
    payload = seen[0].content.decode()
    assert '"kind":"pty"' in payload
    assert '"include_completed":true' in payload
    assert '"limit":25' in payload


@pytest.mark.asyncio
async def test_directory_tree_client_sends_bounded_recursive_options() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "request_id": "tree-list",
                "path": "/srv/project",
                "entries": [],
                "scanned_directories": 1,
                "truncated": False,
            },
        )

    client = GatewayClient(make_settings(), transport=httpx.MockTransport(handler))
    result = await client.list_directory_tree(
        "server-01", "/srv/project", depth=3, include_hidden=True,
        per_directory_limit=25, max_entries=250,
    )
    assert result.scanned_directories == 1
    assert seen[0].url.path.endswith("/files/tree-list")
    payload = seen[0].content.decode()
    assert '"depth":3' in payload
    assert '"include_hidden":true' in payload
    assert '"per_directory_limit":25' in payload
    assert '"max_entries":250' in payload


@pytest.mark.asyncio
async def test_append_file_client_sends_content_and_expected_hash() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "request_id": "append",
                "path": "/srv/notes.txt",
                "bytes_appended": 5,
                "size": 10,
                "sha256": "1" * 64,
            },
        )

    client = GatewayClient(make_settings(), transport=httpx.MockTransport(handler))
    result = await client.append_file(
        "server-01", "/srv/notes.txt", "hello", expected_sha256="0" * 64
    )
    assert result.bytes_appended == 5
    assert seen[0].url.path.endswith("/files/append")
    payload = seen[0].content.decode()
    assert '"content":"hello"' in payload
    assert f'"expected_sha256":"{"0" * 64}"' in payload


@pytest.mark.asyncio
async def test_line_read_client_sends_offset_and_limit() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "request_id": "lines",
                "path": "/srv/app.py",
                "content": "line\n",
                "total_lines": 10,
                "start_line": 4,
                "next_line": 5,
                "eof": False,
            },
        )

    client = GatewayClient(make_settings(), transport=httpx.MockTransport(handler))
    result = await client.read_file_lines("server-01", "/srv/app.py", offset=4, max_lines=1)
    assert result.start_line == 4
    assert seen[0].url.path.endswith("/files/read-lines")
    payload = seen[0].content.decode()
    assert '"offset":4' in payload
    assert '"max_lines":1' in payload


@pytest.mark.asyncio
async def test_command_stdin_client_uses_input_and_close_routes() -> None:
    seen: list[httpx.Request] = []
    session_id = "d" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/input"):
            return httpx.Response(
                200,
                json={
                    "request_id": "input",
                    "session_id": session_id,
                    "accepted_bytes": 6,
                },
            )
        return httpx.Response(
            200,
            json={
                "request_id": "close",
                "session_id": session_id,
                "closed": True,
            },
        )

    client = GatewayClient(make_settings(), transport=httpx.MockTransport(handler))
    written = await client.write_command_input("server-01", session_id, "hello\n")
    closed = await client.close_command_stdin("server-01", session_id)

    assert written.accepted_bytes == 6
    assert closed.closed is True
    assert seen[0].url.path.endswith(f"/{session_id}/input")
    assert seen[0].content == b'{"data":"hello\\n"}'
    assert seen[1].url.path.endswith(f"/{session_id}/stdin/close")


@pytest.mark.asyncio
async def test_command_clients_send_timeout_overrides() -> None:
    requests: list[httpx.Request] = []
    session_id = "e" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/execute"):
            return httpx.Response(200, json={"request_id": "e", "returncode": 0})
        return httpx.Response(
            200,
            json={
                "request_id": "s",
                "session_id": session_id,
                "state": "running",
                "timeout_s": 900.0,
            },
        )

    client = GatewayClient(make_settings(), transport=httpx.MockTransport(handler))
    await client.execute("server-01", ["echo", "ok"], timeout_s=30.0)
    result = await client.start_command_session(
        "server-01", ["echo", "ok"], timeout_s=900.0
    )
    assert result.timeout_s == 900.0
    assert b'"timeout_s":30.0' in requests[0].content
    assert b'"timeout_s":900.0' in requests[1].content


@pytest.mark.asyncio
async def test_command_output_lines_client_sends_line_cursor_payload() -> None:
    seen: list[httpx.Request] = []
    session_id = "ab" * 16

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "request_id": "lines",
                "session_id": session_id,
                "state": "completed",
                "stream": "stderr",
                "content": "e2\ne3\n",
                "total_lines": 3,
                "start_line": 1,
                "next_line": 3,
                "eof": True,
            },
        )

    client = GatewayClient(make_settings(), transport=httpx.MockTransport(handler))
    result = await client.command_session_output_lines(
        "server-01", session_id, stream="stderr", offset=-2, max_lines=10
    )
    assert result.content == "e2\ne3\n"
    assert seen[0].url.path.endswith(f"/commands/sessions/{session_id}/output/lines")
    assert b'"stream":"stderr"' in seen[0].content
    assert b'"offset":-2' in seen[0].content
    assert b'"max_lines":10' in seen[0].content

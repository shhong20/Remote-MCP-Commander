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

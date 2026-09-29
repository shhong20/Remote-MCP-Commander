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
    assert started.state == "running"
    assert status.state == "completed"
    assert cancelled.state == "cancelled"
    assert requests[0].method == "POST"
    assert requests[0].url.path.endswith("/commands/sessions")
    assert requests[1].method == "GET"
    assert requests[1].url.path.endswith(f"/commands/sessions/{session_id}")
    assert requests[2].method == "POST"
    assert requests[2].url.path.endswith(f"/commands/sessions/{session_id}/cancel")
    assert b'"argv":["uptime"]' in requests[0].content

import pytest
from mcp import Client

from remote_mcp_commander.config import Settings
from remote_mcp_commander.mcp_server import build_mcp


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "agent_token": "agent-placeholder-value",
        "control_token": "control-placeholder-value",
        "mcp_transport": "stdio",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_mcp_exposes_minimal_remote_tools() -> None:
    server = build_mcp(make_settings())
    async with Client(server) as client:
        result = await client.list_tools()

    names = {tool.name for tool in result.tools}
    assert names == {
        "list_devices",
        "device_info",
        "ping_device",
        "execute",
        "read_file",
        "write_file",
        "list_processes",
        "service_status",
        "terminate_process",
        "service_action",
    }


@pytest.mark.asyncio
async def test_execute_schema_requires_structured_argv() -> None:
    server = build_mcp(make_settings())
    async with Client(server) as client:
        result = await client.list_tools()

    execute_tool = next(tool for tool in result.tools if tool.name == "execute")
    schema = execute_tool.input_schema
    assert schema["properties"]["argv"]["type"] == "array"
    assert schema["required"] == ["agent_id", "argv"]


def test_streamable_http_requires_mcp_token() -> None:
    settings = make_settings(mcp_transport="streamable-http", mcp_token="")
    with pytest.raises(ValueError, match="COMMANDER_MCP_TOKEN"):
        build_mcp(settings)


def test_streamable_http_builds_with_static_auth() -> None:
    settings = make_settings(
        mcp_transport="streamable-http",
        mcp_token="mcp-placeholder-value",
    )
    server = build_mcp(settings)
    assert server is not None


@pytest.mark.asyncio
async def test_mutation_tools_require_external_approval_fields() -> None:
    server = build_mcp(make_settings())
    async with Client(server) as client:
        result = await client.list_tools()

    tools = {tool.name: tool for tool in result.tools}
    assert "create_approval" not in tools
    terminate_schema = tools["terminate_process"].input_schema
    assert set(terminate_schema["required"]) == {
        "agent_id",
        "pid",
        "expected_create_time_ms",
        "approval_id",
        "approval_secret",
    }
    service_schema = tools["service_action"].input_schema
    assert {"approval_id", "approval_secret"}.issubset(service_schema["required"])

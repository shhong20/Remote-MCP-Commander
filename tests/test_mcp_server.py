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
        "system_health",
        "lookup_port",
        "service_logs",
        "git_status",
        "execute",
        "list_file_roots",
        "list_directory",
        "file_info",
        "read_file",
        "read_files",
        "write_file",
        "edit_file",
        "search_files",
        "start_search",
        "list_searches",
        "get_more_search_results",
        "stop_search",
        "inspect_tree",
        "copy_directory",
        "delete_tree",
        "create_directory",
        "copy_file",
        "move_path",
        "delete_path",
        "list_processes",
        "service_status",
        "terminate_process",
        "signal_process",
        "service_action",
        "start_command",
        "list_sessions",
        "command_status",
        "cancel_command",
        "command_output",
        "discard_command",
        "start_pty",
        "pty_status",
        "write_pty",
        "resize_pty",
        "pty_output",
        "cancel_pty",
        "discard_pty",
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
    signal_schema = tools["signal_process"].input_schema
    assert set(signal_schema["required"]) == {
        "agent_id",
        "pid",
        "expected_create_time_ms",
        "signal",
    }
    assert "approval_id" in signal_schema["properties"]
    assert "approval_secret" in signal_schema["properties"]
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
    pty_schema = tools["start_pty"].input_schema
    assert "approval_id" in pty_schema["properties"]
    assert "approval_secret" in pty_schema["properties"]
    assert "approval_id" not in pty_schema["required"]
    assert "approval_secret" not in pty_schema["required"]


@pytest.mark.asyncio
async def test_command_session_tools_use_structured_contracts() -> None:
    server = build_mcp(make_settings())
    async with Client(server) as client:
        result = await client.list_tools()

    tools = {tool.name: tool for tool in result.tools}
    start_schema = tools["start_command"].input_schema
    assert start_schema["properties"]["argv"]["type"] == "array"
    assert set(start_schema["required"]) == {"agent_id", "argv"}
    list_schema = tools["list_sessions"].input_schema
    assert set(list_schema["required"]) == {"agent_id"}
    assert list_schema["properties"]["kind"]["default"] == "all"
    assert list_schema["properties"]["include_completed"]["default"] is False
    assert list_schema["properties"]["limit"]["default"] == 100
    assert set(tools["command_status"].input_schema["required"]) == {
        "agent_id",
        "session_id",
    }
    assert set(tools["cancel_command"].input_schema["required"]) == {
        "agent_id",
        "session_id",
    }
    assert set(tools["command_output"].input_schema["required"]) == {
        "agent_id",
        "session_id",
    }
    output_properties = tools["command_output"].input_schema["properties"]
    assert output_properties["stdout_offset"]["default"] == 0
    assert output_properties["stderr_offset"]["default"] == 0
    assert output_properties["max_chars"]["default"] == 8192
    assert set(tools["discard_command"].input_schema["required"]) == {
        "agent_id",
        "session_id",
    }


@pytest.mark.asyncio
async def test_pty_tools_use_structured_contracts() -> None:
    server = build_mcp(make_settings())
    async with Client(server) as client:
        result = await client.list_tools()

    tools = {tool.name: tool for tool in result.tools}
    start = tools["start_pty"].input_schema
    assert start["properties"]["argv"]["type"] == "array"
    assert set(start["required"]) == {"agent_id", "argv"}
    assert "approval_id" in start["properties"]
    assert "approval_secret" in start["properties"]
    assert start["properties"]["columns"]["default"] == 80
    assert start["properties"]["rows"]["default"] == 24
    assert set(tools["write_pty"].input_schema["required"]) == {
        "agent_id",
        "session_id",
        "data",
    }
    assert tools["pty_output"].input_schema["properties"]["offset"]["default"] == 0


@pytest.mark.asyncio
async def test_diagnostic_tools_use_structured_contracts() -> None:
    server = build_mcp(make_settings())
    async with Client(server) as client:
        result = await client.list_tools()

    tools = {tool.name: tool for tool in result.tools}
    assert set(tools["system_health"].input_schema["required"]) == {"agent_id"}
    assert set(tools["lookup_port"].input_schema["required"]) == {"agent_id", "port"}
    assert set(tools["service_logs"].input_schema["required"]) == {"agent_id", "unit"}
    assert tools["service_logs"].input_schema["properties"]["lines"]["default"] == 100
    assert set(tools["git_status"].input_schema["required"]) == {"agent_id", "path"}


@pytest.mark.asyncio
async def test_filesystem_discovery_tools_are_bounded_and_structured() -> None:
    server = build_mcp(make_settings())
    async with Client(server) as client:
        result = await client.list_tools()

    tools = {tool.name: tool for tool in result.tools}
    assert set(tools["list_file_roots"].input_schema["required"]) == {"agent_id"}
    assert set(tools["list_directory"].input_schema["required"]) == {"agent_id", "path"}
    properties = tools["list_directory"].input_schema["properties"]
    assert properties["limit"]["default"] == 200
    assert set(tools["file_info"].input_schema["required"]) == {"agent_id", "path"}


@pytest.mark.asyncio
async def test_command_tools_expose_optional_working_directory() -> None:
    server = build_mcp(make_settings())
    async with Client(server) as client:
        result = await client.list_tools()

    tools = {tool.name: tool for tool in result.tools}
    for name in ("execute", "start_command", "start_pty"):
        schema = tools[name].input_schema
        assert "cwd" in schema["properties"]
        assert "cwd" not in schema["required"]
        assert "env" in schema["properties"]
        assert "env" not in schema["required"]


@pytest.mark.asyncio
async def test_read_files_schema_is_bounded() -> None:
    server = build_mcp(make_settings())
    async with Client(server) as client:
        result = await client.list_tools()

    tool = next(item for item in result.tools if item.name == "read_files")
    schema = tool.input_schema
    assert set(schema["required"]) == {"agent_id", "paths"}
    assert schema["properties"]["max_bytes_per_file"]["default"] == 32768
    assert schema["properties"]["max_total_bytes"]["default"] == 262144


@pytest.mark.asyncio
async def test_stateful_search_tools_use_bounded_schema() -> None:
    server = build_mcp(make_settings())
    async with Client(server) as client:
        result = await client.list_tools()

    tools = {tool.name: tool for tool in result.tools}
    start = tools["start_search"].input_schema
    assert set(start["required"]) == {"agent_id", "root", "query"}
    assert start["properties"]["include_hidden"]["default"] is False
    assert start["properties"]["page_size"]["default"] == 50
    assert start["properties"]["max_results"]["default"] == 1000
    listing = tools["list_searches"].input_schema
    assert set(listing["required"]) == {"agent_id"}
    more = tools["get_more_search_results"].input_schema
    assert set(more["required"]) == {"agent_id", "session_id"}
    assert more["properties"]["offset"]["default"] is None
    assert more["properties"]["limit"]["default"] == 50
    stop = tools["stop_search"].input_schema
    assert set(stop["required"]) == {"agent_id", "session_id"}


@pytest.mark.asyncio
async def test_recursive_tree_tools_require_inspection_fingerprint() -> None:
    server = build_mcp(make_settings())
    async with Client(server) as client:
        result = await client.list_tools()

    tools = {tool.name: tool for tool in result.tools}
    inspect = tools["inspect_tree"].input_schema
    assert set(inspect["required"]) == {"agent_id", "path"}
    copy = tools["copy_directory"].input_schema
    assert {"agent_id", "source", "destination", "expected_tree_sha256"} == set(copy["required"])
    delete = tools["delete_tree"].input_schema
    assert {"agent_id", "path", "expected_tree_sha256"} == set(delete["required"])

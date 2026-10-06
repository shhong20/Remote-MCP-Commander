import asyncio
import base64

import httpx
import pytest
from mcp import Client

from remote_mcp_commander.config import Settings
from remote_mcp_commander.gateway.client import GatewayAPIError, GatewayClient
from remote_mcp_commander.mcp_server import (
    build_mcp,
    execute_many_commands,
    mutate_many_paths,
    preview_many_documents,
    read_multiple_file_contents,
)
from remote_mcp_commander.protocol import (
    BatchCommandSpec,
    BatchDocumentPreviewSpec,
    BatchPathMutationSpec,
    CommandResult,
    DocumentPreviewResult,
    FileReadResult,
    ImagePreviewResult,
    MultiFileReadSpec,
    PathMutationResult,
)


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
        "list_commands",
        "device_info",
        "get_runtime_config",
        "get_operator_config",
        "set_operator_config",
        "get_recent_activity",
        "get_usage_stats",
        "ping_device",
        "system_health",
        "lookup_port",
        "service_logs",
        "git_status",
        "execute",
        "execute_many",
        "list_file_roots",
        "list_directory",
        "list_directory_tree",
        "file_info",
        "preview_document",
        "replace_docx_text",
        "edit_xlsx_range",
        "compose_pdf_pages",
        "create_pdf_from_markdown",
        "rewrite_pdf_with_markdown",
        "inspect_archive",
        "extract_archive",
        "create_archive",
        "preview_documents",
        "preview_image",
        "read_multiple_files",
        "read_file",
        "read_binary",
        "read_file_lines",
        "tail_file",
        "read_files",
        "write_file",
        "write_binary",
        "start_file_upload",
        "upload_file_chunk",
        "finish_file_upload",
        "start_file_download",
        "download_file_chunk",
        "file_transfer_status",
        "close_file_transfer",
        "append_file",
        "edit_file",
        "search_files",
        "start_search",
        "list_searches",
        "get_more_search_results",
        "stop_search",
        "inspect_tree",
        "copy_directory",
        "delete_tree",
        "mutate_paths",
        "create_directory",
        "copy_file",
        "move_path",
        "delete_path",
        "list_processes",
        "process_info",
        "service_status",
        "terminate_process",
        "signal_process",
        "signal_session",
        "service_action",
        "start_command",
        "list_sessions",
        "command_status",
        "cancel_command",
        "write_command_input",
        "close_command_stdin",
        "command_output",
        "command_output_lines",
        "discard_command",
        "start_pty",
        "pty_status",
        "write_pty",
        "resize_pty",
        "pty_output",
        "pty_output_lines",
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
    assert "timeout_s" in schema["properties"]
    assert "timeout_s" not in schema["required"]

    preview_schema = next(
        tool for tool in result.tools if tool.name == "preview_document"
    ).input_schema
    assert set(preview_schema["required"]) == {"agent_id", "path"}
    assert preview_schema["properties"]["page"]["default"] == 1
    assert preview_schema["properties"]["max_pages"]["default"] == 5
    assert preview_schema["properties"]["cell_range"]["default"] is None
    assert "cell_range" not in preview_schema.get("required", [])
    assert preview_schema["properties"]["max_rows"]["default"] == 200

    document_batch_schema = next(
        tool for tool in result.tools if tool.name == "preview_documents"
    ).input_schema
    assert set(document_batch_schema["required"]) == {"agent_id", "documents"}
    assert document_batch_schema["properties"]["documents"]["minItems"] == 1
    assert document_batch_schema["properties"]["documents"]["maxItems"] == 8
    assert document_batch_schema["properties"]["max_concurrency"]["default"] == 4
    assert document_batch_schema["properties"]["max_concurrency"]["minimum"] == 1
    assert document_batch_schema["properties"]["max_concurrency"]["maximum"] == 4
    batch_spec = document_batch_schema["$defs"]["BatchDocumentPreviewSpec"]
    assert batch_spec["required"] == ["path"]
    assert batch_spec["properties"]["max_chars"]["default"] == 32_768
    assert batch_spec["properties"]["max_chars"]["maximum"] == 65_536

    multi_read_schema = next(
        tool for tool in result.tools if tool.name == "read_multiple_files"
    ).input_schema
    assert set(multi_read_schema["required"]) == {"agent_id", "files"}
    assert multi_read_schema["properties"]["files"]["minItems"] == 1
    assert multi_read_schema["properties"]["files"]["maxItems"] == 8
    assert multi_read_schema["properties"]["max_concurrency"]["default"] == 4
    assert multi_read_schema["properties"]["max_concurrency"]["minimum"] == 1
    assert multi_read_schema["properties"]["max_concurrency"]["maximum"] == 4
    multi_spec = multi_read_schema["$defs"]["MultiFileReadSpec"]
    assert multi_spec["required"] == ["path"]
    assert multi_spec["properties"]["max_chars"]["default"] == 32_768
    assert multi_spec["properties"]["max_chars"]["maximum"] == 65_536
    assert multi_spec["properties"]["max_bytes"]["default"] == 32_768
    assert multi_spec["properties"]["max_bytes"]["maximum"] == 65_536

    docx_edit_schema = next(
        tool for tool in result.tools if tool.name == "replace_docx_text"
    ).input_schema
    assert set(docx_edit_schema["required"]) == {
        "agent_id", "path", "old_text", "new_text", "expected_sha256"
    }
    assert docx_edit_schema["properties"]["path"]["maxLength"] == 4096
    assert docx_edit_schema["properties"]["old_text"]["maxLength"] == 8192
    assert docx_edit_schema["properties"]["new_text"]["maxLength"] == 8192
    assert docx_edit_schema["properties"]["expected_sha256"]["pattern"] == "^[a-f0-9]{64}$"
    assert docx_edit_schema["properties"]["expected_replacements"]["maximum"] == 100

    xlsx_edit_schema = next(
        tool for tool in result.tools if tool.name == "edit_xlsx_range"
    ).input_schema
    assert set(xlsx_edit_schema["required"]) == {
        "agent_id", "path", "sheet", "cell_range", "values", "expected_sha256"
    }
    assert xlsx_edit_schema["properties"]["values"]["maxItems"] == 100
    row_schema = xlsx_edit_schema["properties"]["values"]["items"]
    assert row_schema["maxItems"] == 32
    assert row_schema["items"]["anyOf"][0]["maxLength"] == 512
    assert xlsx_edit_schema["properties"]["expected_sha256"]["pattern"] == "^[a-f0-9]{64}$"

    pdf_compose_schema = next(
        tool for tool in result.tools if tool.name == "compose_pdf_pages"
    ).input_schema
    assert set(pdf_compose_schema["required"]) == {"agent_id", "output_path", "sources"}
    assert pdf_compose_schema["properties"]["sources"]["minItems"] == 1
    assert pdf_compose_schema["properties"]["sources"]["maxItems"] == 16
    assert pdf_compose_schema["properties"]["overwrite"]["default"] is False
    assert pdf_compose_schema["properties"]["expected_sha256"]["default"] is None
    pdf_source = pdf_compose_schema["$defs"]["PdfPageSource"]
    assert pdf_source["properties"]["start_page"]["default"] == 1
    assert pdf_source["properties"]["start_page"]["maximum"] == 100_000
    assert pdf_source["properties"]["end_page"]["anyOf"][0]["maximum"] == 100_000

    pdf_render_schema = next(
        tool for tool in result.tools if tool.name == "create_pdf_from_markdown"
    ).input_schema
    assert set(pdf_render_schema["required"]) == {"agent_id", "output_path", "markdown"}
    assert pdf_render_schema["properties"]["markdown"]["maxLength"] == 262_144
    assert pdf_render_schema["properties"]["overwrite"]["default"] is False
    assert pdf_render_schema["properties"]["expected_sha256"]["default"] is None
    assert (
        pdf_render_schema["properties"]["expected_sha256"]["anyOf"][0]["pattern"]
        == "^[a-f0-9]{64}$"
    )

    pdf_rewrite_schema = next(
        tool for tool in result.tools if tool.name == "rewrite_pdf_with_markdown"
    ).input_schema
    assert set(pdf_rewrite_schema["required"]) == {
        "agent_id",
        "source_path",
        "output_path",
        "segments",
        "expected_source_sha256",
    }
    assert pdf_rewrite_schema["properties"]["segments"]["minItems"] == 1
    assert pdf_rewrite_schema["properties"]["segments"]["maxItems"] == 32
    assert pdf_rewrite_schema["properties"]["overwrite"]["default"] is False
    assert pdf_rewrite_schema["properties"]["expected_output_sha256"]["default"] is None
    source_segment = pdf_rewrite_schema["$defs"]["PdfSourcePagesSegment"]
    markdown_segment = pdf_rewrite_schema["$defs"]["PdfMarkdownSegment"]
    assert source_segment["properties"]["kind"]["const"] == "source_pages"
    assert markdown_segment["properties"]["kind"]["const"] == "markdown"
    assert markdown_segment["properties"]["markdown"]["maxLength"] == 131_072

    binary_read_schema = next(
        tool for tool in result.tools if tool.name == "read_binary"
    ).input_schema
    assert set(binary_read_schema["required"]) == {"agent_id", "path"}
    assert binary_read_schema["properties"]["offset"]["default"] == 0
    assert binary_read_schema["properties"]["offset"]["maximum"] == 1_048_576
    assert binary_read_schema["properties"]["max_bytes"]["default"] == 262_144
    assert binary_read_schema["properties"]["max_bytes"]["maximum"] == 262_144

    binary_write_schema = next(
        tool for tool in result.tools if tool.name == "write_binary"
    ).input_schema
    assert set(binary_write_schema["required"]) == {"agent_id", "path", "data_base64"}
    assert binary_write_schema["properties"]["data_base64"]["maxLength"] == 1_398_104
    assert binary_write_schema["properties"]["overwrite"]["default"] is False
    assert binary_write_schema["properties"]["expected_sha256"]["default"] is None

    upload_start_schema = next(
        tool for tool in result.tools if tool.name == "start_file_upload"
    ).input_schema
    assert set(upload_start_schema["required"]) == {"agent_id", "path", "size", "sha256"}
    assert upload_start_schema["properties"]["size"]["maximum"] == 1_073_741_824
    assert upload_start_schema["properties"]["overwrite"]["default"] is False

    upload_chunk_schema = next(
        tool for tool in result.tools if tool.name == "upload_file_chunk"
    ).input_schema
    assert set(upload_chunk_schema["required"]) == {
        "agent_id", "session_id", "offset", "data_base64"
    }
    assert upload_chunk_schema["properties"]["data_base64"]["maxLength"] == 349_528

    download_chunk_schema = next(
        tool for tool in result.tools if tool.name == "download_file_chunk"
    ).input_schema
    assert set(download_chunk_schema["required"]) == {"agent_id", "session_id"}
    assert download_chunk_schema["properties"]["offset"]["default"] == 0
    assert download_chunk_schema["properties"]["max_bytes"]["maximum"] == 262_144

    transfer_status_schema = next(
        tool for tool in result.tools if tool.name == "file_transfer_status"
    ).input_schema
    assert set(transfer_status_schema["required"]) == {"agent_id", "session_id"}

    mutation_batch_schema = next(
        tool for tool in result.tools if tool.name == "mutate_paths"
    ).input_schema
    assert set(mutation_batch_schema["required"]) == {"agent_id", "operations"}
    assert mutation_batch_schema["properties"]["operations"]["minItems"] == 1
    assert mutation_batch_schema["properties"]["operations"]["maxItems"] == 16
    assert mutation_batch_schema["properties"]["stop_on_error"]["default"] is True
    mutation_spec = mutation_batch_schema["$defs"]["BatchPathMutationSpec"]
    assert set(mutation_spec["required"]) == {"operation", "path"}
    assert mutation_spec["properties"]["parents"]["default"] is False
    assert mutation_spec["properties"]["overwrite"]["default"] is False

    batch_schema = next(
        tool for tool in result.tools if tool.name == "execute_many"
    ).input_schema
    assert set(batch_schema["required"]) == {"agent_id", "commands"}
    assert batch_schema["properties"]["commands"]["minItems"] == 1
    assert batch_schema["properties"]["commands"]["maxItems"] == 16
    assert batch_schema["properties"]["max_concurrency"]["default"] == 4
    assert batch_schema["properties"]["max_concurrency"]["minimum"] == 1
    assert batch_schema["properties"]["max_concurrency"]["maximum"] == 4


    config_schema = next(
        tool for tool in result.tools if tool.name == "get_runtime_config"
    ).input_schema
    assert config_schema["required"] == ["agent_id"]

    operator_get_schema = next(
        tool for tool in result.tools if tool.name == "get_operator_config"
    ).input_schema
    assert operator_get_schema.get("required", []) == []

    operator_set_schema = next(
        tool for tool in result.tools if tool.name == "set_operator_config"
    ).input_schema
    assert operator_set_schema["required"] == ["key"]
    assert operator_set_schema["properties"]["value"]["default"] is None
    assert "allowed_roots_json" not in operator_set_schema["properties"]["key"]["enum"]
    assert "control_token" not in operator_set_schema["properties"]["key"]["enum"]

    activity_schema = next(
        tool for tool in result.tools if tool.name == "get_recent_activity"
    ).input_schema
    assert activity_schema["properties"]["limit"]["default"] == 50
    assert "event" not in activity_schema.get("required", [])
    assert "agent_id" not in activity_schema.get("required", [])

    usage_schema = next(
        tool for tool in result.tools if tool.name == "get_usage_stats"
    ).input_schema
    assert usage_schema.get("required", []) == []
    assert usage_schema["properties"]["agent_id"]["default"] is None


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
    assert "timeout_s" in start_schema["properties"]
    assert "timeout_s" not in start_schema["required"]
    tail_schema = tools["tail_file"].input_schema
    assert tail_schema["properties"]["lines"]["default"] == 100
    assert tail_schema["properties"]["max_bytes"]["default"] == 262144

    tree_schema = tools["list_directory_tree"].input_schema
    assert tree_schema["properties"]["depth"]["default"] == 2
    assert tree_schema["properties"]["include_hidden"]["default"] is False
    assert tree_schema["properties"]["per_directory_limit"]["default"] == 100
    assert tree_schema["properties"]["max_entries"]["default"] == 1000

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
    input_schema = tools["write_command_input"].input_schema
    assert set(input_schema["required"]) == {"agent_id", "session_id", "data"}
    close_schema = tools["close_command_stdin"].input_schema
    assert set(close_schema["required"]) == {"agent_id", "session_id"}
    output_properties = tools["command_output"].input_schema["properties"]
    assert output_properties["stdout_offset"]["default"] == 0
    assert output_properties["stderr_offset"]["default"] == 0
    assert output_properties["max_chars"]["default"] == 8192
    line_schema = tools["command_output_lines"].input_schema
    assert set(line_schema["required"]) == {"agent_id", "session_id"}
    assert line_schema["properties"]["stream"]["default"] == "stdout"
    assert line_schema["properties"]["offset"]["default"] == 0
    assert line_schema["properties"]["max_lines"]["default"] == 200
    assert line_schema["properties"]["wait_ms"]["default"] == 0
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
    line_schema = tools["pty_output_lines"].input_schema
    assert set(line_schema["required"]) == {"agent_id", "session_id"}
    assert line_schema["properties"]["offset"]["default"] == 0
    assert line_schema["properties"]["max_lines"]["default"] == 200
    assert line_schema["properties"]["wait_ms"]["default"] == 0


@pytest.mark.asyncio
async def test_diagnostic_tools_use_structured_contracts() -> None:
    server = build_mcp(make_settings())
    async with Client(server) as client:
        result = await client.list_tools()

    tools = {tool.name: tool for tool in result.tools}
    assert set(tools["system_health"].input_schema["required"]) == {"agent_id"}
    assert set(tools["lookup_port"].input_schema["required"]) == {"agent_id", "port"}
    assert set(tools["process_info"].input_schema["required"]) == {"agent_id", "pid"}
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


@pytest.mark.asyncio
async def test_signal_session_schema_is_session_bound() -> None:
    server = build_mcp(make_settings())
    async with Client(server) as client:
        result = await client.list_tools()
    tools = {tool.name: tool for tool in result.tools}
    schema = tools["signal_session"].input_schema
    assert set(schema["required"]) == {"agent_id", "session_id", "kind", "signal"}
    assert "pid" not in schema["properties"]
    assert "approval_id" not in schema["properties"]


@pytest.mark.asyncio
async def test_list_commands_schema_requires_only_agent_id() -> None:
    server = build_mcp(make_settings())
    async with Client(server) as client:
        result = await client.list_tools()
    tool = next(item for item in result.tools if item.name == "list_commands")
    assert set(tool.input_schema["required"]) == {"agent_id"}


@pytest.mark.asyncio
async def test_execute_many_preserves_input_order_and_bounds_concurrency(monkeypatch) -> None:
    active = 0
    max_active = 0

    async def fake_execute(self, agent_id, argv, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return CommandResult(request_id=argv[1], returncode=0, stdout=argv[1])

    monkeypatch.setattr(GatewayClient, "execute", fake_execute)
    commands = [BatchCommandSpec(argv=["echo", str(i)]) for i in range(6)]
    result = await execute_many_commands(
        make_settings(), "server-01", commands, max_concurrency=2
    )
    assert [item.index for item in result.results] == list(range(6))
    assert [item.result.stdout for item in result.results if item.result] == [
        str(i) for i in range(6)
    ]
    assert max_active == 2


@pytest.mark.asyncio
async def test_execute_many_isolates_gateway_policy_failure(monkeypatch) -> None:
    async def fake_execute(self, agent_id, argv, **kwargs):
        if argv[0] == "blocked":
            raise GatewayAPIError(403, "policy denied")
        return CommandResult(request_id="ok", returncode=0, stdout="ok")

    monkeypatch.setattr(GatewayClient, "execute", fake_execute)
    result = await execute_many_commands(
        make_settings(),
        "server-01",
        [BatchCommandSpec(argv=["blocked"]), BatchCommandSpec(argv=["echo", "ok"])],
        max_concurrency=2,
    )
    assert result.results[0].status_code == 403
    assert result.results[0].error == "policy denied"
    assert result.results[0].result is None
    assert result.results[1].result is not None
    assert result.results[1].result.stdout == "ok"


@pytest.mark.asyncio
async def test_preview_many_preserves_order_and_bounds_concurrency(monkeypatch) -> None:
    active = 0
    max_active = 0
    seen: list[tuple[str, dict[str, object]]] = []

    async def fake_preview(self, agent_id, path, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        seen.append((path, kwargs))
        await asyncio.sleep(0.01)
        active -= 1
        return DocumentPreviewResult(
            request_id=path,
            path=path,
            kind="docx",
            content=path,
        )

    monkeypatch.setattr(GatewayClient, "preview_document", fake_preview)
    documents = [
        BatchDocumentPreviewSpec(path=f"/home/ubuntu/doc-{index}.docx")
        for index in range(6)
    ]
    result = await preview_many_documents(
        make_settings(),
        "server-01",
        documents,
        max_concurrency=2,
    )

    assert [item.index for item in result.results] == list(range(6))
    assert [item.path for item in result.results] == [item.path for item in documents]
    assert [item.result.content for item in result.results if item.result] == [
        item.path for item in documents
    ]
    assert max_active == 2
    assert all(kwargs["max_chars"] == 32_768 for _, kwargs in seen)


@pytest.mark.asyncio
async def test_preview_many_forwards_navigation_fields(monkeypatch) -> None:
    seen: list[dict[str, object]] = []

    async def fake_preview(self, agent_id, path, **kwargs):
        seen.append(kwargs)
        return DocumentPreviewResult(
            request_id="xlsx",
            path=path,
            kind="xlsx",
            content="ok",
        )

    monkeypatch.setattr(GatewayClient, "preview_document", fake_preview)
    result = await preview_many_documents(
        make_settings(),
        "server-01",
        [
            BatchDocumentPreviewSpec(
                path="/home/ubuntu/report.xlsx",
                sheet="Data",
                cell_range="B2:F40",
                max_rows=25,
                max_chars=4096,
            )
        ],
        max_concurrency=1,
    )

    assert result.results[0].result is not None
    assert seen == [
        {
            "page": 1,
            "max_pages": 5,
            "sheet": "Data",
            "cell_range": "B2:F40",
            "max_rows": 25,
            "max_chars": 4096,
        }
    ]


@pytest.mark.asyncio
async def test_preview_many_isolates_gateway_failure(monkeypatch) -> None:
    async def fake_preview(self, agent_id, path, **kwargs):
        if path.endswith("blocked.pdf"):
            raise GatewayAPIError(403, "document denied")
        return DocumentPreviewResult(
            request_id="ok",
            path=path,
            kind="pdf",
            content="ok",
        )

    monkeypatch.setattr(GatewayClient, "preview_document", fake_preview)
    result = await preview_many_documents(
        make_settings(),
        "server-01",
        [
            BatchDocumentPreviewSpec(path="/home/ubuntu/blocked.pdf"),
            BatchDocumentPreviewSpec(path="/home/ubuntu/ok.pdf"),
        ],
        max_concurrency=2,
    )

    assert result.results[0].status_code == 403
    assert result.results[0].error == "document denied"
    assert result.results[0].result is None
    assert result.results[1].result is not None
    assert result.results[1].result.content == "ok"


@pytest.mark.asyncio
async def test_preview_image_returns_text_metadata_and_image_content(monkeypatch) -> None:
    raw = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    async def fake_preview(self, agent_id, path):
        return ImagePreviewResult(
            request_id="image",
            path=path,
            format="png",
            mime_type="image/png",
            data_base64=base64.b64encode(raw).decode("ascii"),
            size=len(raw),
            width=1,
            height=1,
            sha256="a" * 64,
        )

    monkeypatch.setattr(GatewayClient, "preview_image", fake_preview)
    server = build_mcp(make_settings())
    async with Client(server) as client:
        result = await client.call_tool(
            "preview_image",
            {"agent_id": "server-01", "path": "/home/ubuntu/sample.png"},
        )

    assert result.is_error is False
    assert len(result.content) == 2
    assert result.content[0].type == "text"
    assert "image/png | 1x1" in result.content[0].text
    assert result.content[1].type == "image"
    assert result.content[1].mime_type == "image/png"
    assert base64.b64decode(result.content[1].data) == raw


@pytest.mark.asyncio
async def test_preview_image_rejection_becomes_tool_error(monkeypatch) -> None:
    async def fake_preview(self, agent_id, path):
        return ImagePreviewResult(
            request_id="image",
            rejected=True,
            error="image exceeds 1 MiB preview limit",
        )

    monkeypatch.setattr(GatewayClient, "preview_image", fake_preview)
    server = build_mcp(make_settings())
    async with Client(server) as client:
        result = await client.call_tool(
            "preview_image",
            {"agent_id": "server-01", "path": "/home/ubuntu/large.png"},
        )

    assert result.is_error is True
    assert "1 MiB" in result.content[0].text


@pytest.mark.asyncio
async def test_read_multiple_files_routes_mixed_inputs_and_preserves_order(monkeypatch) -> None:
    raw = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    active = 0
    max_active = 0
    routes: list[tuple[str, str]] = []

    async def enter(route: str, path: str) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        routes.append((route, path))
        await asyncio.sleep(0.01)
        active -= 1

    async def fake_read_file(self, agent_id, path, **kwargs):
        await enter("text", path)
        return FileReadResult(
            request_id="text",
            path=path,
            content="hello text",
            size=10,
            next_offset=10,
            eof=True,
            sha256="a" * 64,
        )

    async def fake_preview_document(self, agent_id, path, **kwargs):
        await enter("document", path)
        return DocumentPreviewResult(
            request_id="doc",
            path=path,
            kind="xlsx",
            content="A\tB\n",
            size=20,
            sha256="b" * 64,
            sheet="Data",
            rows_returned=1,
        )

    async def fake_preview_image(self, agent_id, path):
        await enter("image", path)
        return ImagePreviewResult(
            request_id="image",
            path=path,
            format="png",
            mime_type="image/png",
            data_base64=base64.b64encode(raw).decode("ascii"),
            size=len(raw),
            width=1,
            height=1,
            sha256="c" * 64,
        )

    monkeypatch.setattr(GatewayClient, "read_file", fake_read_file)
    monkeypatch.setattr(GatewayClient, "preview_document", fake_preview_document)
    monkeypatch.setattr(GatewayClient, "preview_image", fake_preview_image)

    result = await read_multiple_file_contents(
        make_settings(),
        "server-01",
        [
            MultiFileReadSpec(path="/home/ubuntu/notes.txt"),
            MultiFileReadSpec(path="/home/ubuntu/report.XLSX", sheet="Data"),
            MultiFileReadSpec(path="/home/ubuntu/pixel.PNG"),
        ],
        max_concurrency=2,
    )

    assert max_active == 2
    assert routes == [
        ("text", "/home/ubuntu/notes.txt"),
        ("document", "/home/ubuntu/report.XLSX"),
        ("image", "/home/ubuntu/pixel.PNG"),
    ]
    assert result[0].startswith("[0] /home/ubuntu/notes.txt | text")
    assert "hello text" in result[0]
    assert result[1].startswith("[1] /home/ubuntu/report.XLSX | kind=xlsx")
    assert "A\tB" in result[1]
    assert result[2].startswith("[2] /home/ubuntu/pixel.PNG | image/png | 1x1")
    assert result[3].data is not None


@pytest.mark.asyncio
async def test_read_multiple_files_isolates_item_failures(monkeypatch) -> None:
    async def fake_read_file(self, agent_id, path, **kwargs):
        raise GatewayAPIError(403, "text denied")

    async def fake_preview_document(self, agent_id, path, **kwargs):
        return DocumentPreviewResult(
            request_id="doc",
            rejected=True,
            error="document denied",
        )

    async def fake_preview_image(self, agent_id, path):
        return ImagePreviewResult(
            request_id="image",
            rejected=True,
            error="invalid PNG signature",
        )

    monkeypatch.setattr(GatewayClient, "read_file", fake_read_file)
    monkeypatch.setattr(GatewayClient, "preview_document", fake_preview_document)
    monkeypatch.setattr(GatewayClient, "preview_image", fake_preview_image)

    result = await read_multiple_file_contents(
        make_settings(),
        "server-01",
        [
            MultiFileReadSpec(path="/home/ubuntu/a.txt"),
            MultiFileReadSpec(path="/home/ubuntu/b.pdf"),
            MultiFileReadSpec(path="/home/ubuntu/c.png"),
        ],
        max_concurrency=3,
    )

    assert len(result) == 3
    assert "ERROR 403: text denied" in result[0]
    assert "ERROR: document denied" in result[1]
    assert "ERROR: invalid PNG signature" in result[2]


@pytest.mark.asyncio
async def test_read_multiple_files_bounds_total_image_content(monkeypatch) -> None:
    raw = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    async def fake_preview_image(self, agent_id, path):
        return ImagePreviewResult(
            request_id=path,
            path=path,
            format="png",
            mime_type="image/png",
            data_base64=base64.b64encode(raw).decode("ascii"),
            size=len(raw),
            width=1,
            height=1,
            sha256="d" * 64,
        )

    monkeypatch.setattr(GatewayClient, "preview_image", fake_preview_image)
    monkeypatch.setitem(
        read_multiple_file_contents.__globals__,
        "_MULTI_READ_MAX_IMAGE_BYTES",
        len(raw),
    )

    result = await read_multiple_file_contents(
        make_settings(),
        "server-01",
        [
            MultiFileReadSpec(path="/home/ubuntu/one.png"),
            MultiFileReadSpec(path="/home/ubuntu/two.png"),
        ],
        max_concurrency=2,
    )

    assert len(result) == 3
    assert result[0].startswith("[0] /home/ubuntu/one.png | image/png")
    assert result[1].data is not None
    assert "2 MiB batch image budget was exceeded" in result[2]


@pytest.mark.asyncio
async def test_read_multiple_files_isolates_gateway_transport_and_validation_failures(
    monkeypatch,
) -> None:
    async def fake_read_file(self, agent_id, path, **kwargs):
        if path.endswith("network.txt"):
            raise httpx.ConnectError("connection failed")
        raise ValueError("malformed gateway payload")

    monkeypatch.setattr(GatewayClient, "read_file", fake_read_file)

    result = await read_multiple_file_contents(
        make_settings(),
        "server-01",
        [
            MultiFileReadSpec(path="/home/ubuntu/network.txt"),
            MultiFileReadSpec(path="/home/ubuntu/invalid.txt"),
        ],
        max_concurrency=2,
    )

    assert len(result) == 2
    assert "ERROR 503: gateway request failed" in result[0]
    assert "ERROR 502: gateway response validation failed" in result[1]


@pytest.mark.asyncio
async def test_mutate_many_paths_runs_sequentially_in_input_order(monkeypatch) -> None:
    active = 0
    max_active = 0
    seen: list[tuple[str, str, str | None, bool, bool]] = []

    async def fake_mutate(
        self, agent_id, operation, path, *, destination=None, parents=False, overwrite=False
    ):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        seen.append((operation, path, destination, parents, overwrite))
        await asyncio.sleep(0.01)
        active -= 1
        return PathMutationResult(
            request_id=path,
            operation=operation,
            path=path,
            destination=destination,
            changed=True,
        )

    monkeypatch.setattr(GatewayClient, "mutate_path", fake_mutate)
    operations = [
        BatchPathMutationSpec(operation="mkdir", path="/home/ubuntu/work", parents=True),
        BatchPathMutationSpec(
            operation="copy",
            path="/home/ubuntu/a.bin",
            destination="/home/ubuntu/work/a.bin",
        ),
        BatchPathMutationSpec(
            operation="move",
            path="/home/ubuntu/work/a.bin",
            destination="/home/ubuntu/work/b.bin",
            overwrite=True,
        ),
        BatchPathMutationSpec(operation="delete", path="/home/ubuntu/work/b.bin"),
    ]

    result = await mutate_many_paths(
        make_settings(), "server-01", operations, stop_on_error=True
    )

    assert result.completed_count == 4
    assert result.stopped_early is False
    assert [item.index for item in result.results] == [0, 1, 2, 3]
    assert [item.operation for item in result.results] == ["mkdir", "copy", "move", "delete"]
    assert seen == [
        ("mkdir", "/home/ubuntu/work", None, True, False),
        ("copy", "/home/ubuntu/a.bin", "/home/ubuntu/work/a.bin", False, False),
        ("move", "/home/ubuntu/work/a.bin", "/home/ubuntu/work/b.bin", False, True),
        ("delete", "/home/ubuntu/work/b.bin", None, False, False),
    ]
    assert max_active == 1


@pytest.mark.asyncio
async def test_mutate_many_paths_stops_after_rejected_result_by_default(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_mutate(self, agent_id, operation, path, **kwargs):
        calls.append(path)
        if path.endswith("missing.bin"):
            return PathMutationResult(
                request_id="reject",
                operation=operation,
                path=path,
                rejected=True,
                error="source path does not exist",
            )
        return PathMutationResult(
            request_id="ok", operation=operation, path=path, changed=True
        )

    monkeypatch.setattr(GatewayClient, "mutate_path", fake_mutate)
    result = await mutate_many_paths(
        make_settings(),
        "server-01",
        [
            BatchPathMutationSpec(operation="delete", path="/home/ubuntu/first.bin"),
            BatchPathMutationSpec(operation="delete", path="/home/ubuntu/missing.bin"),
            BatchPathMutationSpec(operation="delete", path="/home/ubuntu/last.bin"),
        ],
        stop_on_error=True,
    )

    assert calls == ["/home/ubuntu/first.bin", "/home/ubuntu/missing.bin"]
    assert result.completed_count == 2
    assert result.stopped_early is True
    assert result.results[1].result is not None
    assert result.results[1].result.rejected is True


@pytest.mark.asyncio
async def test_mutate_many_paths_can_continue_after_item_failure(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_mutate(self, agent_id, operation, path, **kwargs):
        calls.append(path)
        if path.endswith("blocked.bin"):
            raise GatewayAPIError(403, "mutation denied")
        return PathMutationResult(
            request_id="ok", operation=operation, path=path, changed=True
        )

    monkeypatch.setattr(GatewayClient, "mutate_path", fake_mutate)
    result = await mutate_many_paths(
        make_settings(),
        "server-01",
        [
            BatchPathMutationSpec(operation="delete", path="/home/ubuntu/blocked.bin"),
            BatchPathMutationSpec(operation="delete", path="/home/ubuntu/next.bin"),
        ],
        stop_on_error=False,
    )

    assert calls == ["/home/ubuntu/blocked.bin", "/home/ubuntu/next.bin"]
    assert result.completed_count == 2
    assert result.stopped_early is False
    assert result.results[0].status_code == 403
    assert result.results[0].error == "mutation denied"
    assert result.results[1].result is not None
    assert result.results[1].result.changed is True


def test_batch_path_mutation_spec_rejects_irrelevant_fields() -> None:
    with pytest.raises(ValueError, match="destination is required"):
        BatchPathMutationSpec(operation="copy", path="/home/ubuntu/a.bin")
    with pytest.raises(ValueError, match="parents is only valid"):
        BatchPathMutationSpec(
            operation="move",
            path="/home/ubuntu/a.bin",
            destination="/home/ubuntu/b.bin",
            parents=True,
        )
    with pytest.raises(ValueError, match="destination is only valid"):
        BatchPathMutationSpec(
            operation="delete",
            path="/home/ubuntu/a.bin",
            destination="/home/ubuntu/b.bin",
        )
    with pytest.raises(ValueError, match="overwrite is only valid"):
        BatchPathMutationSpec(operation="mkdir", path="/home/ubuntu/new", overwrite=True)


@pytest.mark.asyncio
async def test_mutate_many_paths_isolates_transport_error(monkeypatch) -> None:
    calls = 0

    async def fake_mutate(self, agent_id, operation, path, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("connection failed")
        return PathMutationResult(
            request_id="ok", operation=operation, path=path, changed=True
        )

    monkeypatch.setattr(GatewayClient, "mutate_path", fake_mutate)
    result = await mutate_many_paths(
        make_settings(),
        "server-01",
        [
            BatchPathMutationSpec(operation="delete", path="/home/ubuntu/first.bin"),
            BatchPathMutationSpec(operation="delete", path="/home/ubuntu/second.bin"),
        ],
        stop_on_error=False,
    )

    assert result.results[0].status_code == 503
    assert result.results[0].error == "gateway request failed"
    assert result.results[1].result is not None
    assert result.results[1].result.changed is True

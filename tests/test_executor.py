import pytest

from remote_mcp_commander.agent.executor import execute_argv


@pytest.mark.asyncio
async def test_rejects_safe_executable_not_in_operator_allowlist() -> None:
    result = await execute_argv(
        "req-1",
        ["hostname"],
        allowlist={"echo"},
        timeout_s=1,
        max_output_bytes=1024,
    )
    assert result.rejected is True
    assert result.returncode is None
    assert "not allowed" in (result.error or "")


@pytest.mark.asyncio
async def test_executes_profiled_binary_without_shell() -> None:
    result = await execute_argv(
        "req-2",
        ["echo", "ok"],
        allowlist={"echo"},
        timeout_s=2,
        max_output_bytes=1024,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "ok"
    assert result.rejected is False


@pytest.mark.asyncio
async def test_truncates_output() -> None:
    result = await execute_argv(
        "req-3",
        ["echo", "x" * 100],
        allowlist={"echo"},
        timeout_s=2,
        max_output_bytes=10,
    )
    assert result.returncode == 0
    assert len(result.stdout.encode()) <= 10


@pytest.mark.asyncio
async def test_generic_execute_rejects_unprofiled_interpreter_even_if_allowlisted() -> None:
    result = await execute_argv(
        "req-4",
        ["python3", "-c", "print('unsafe')"],
        allowlist={"python3"},
        timeout_s=2,
        max_output_bytes=1024,
    )
    assert result.rejected is True
    assert "no safe generic profile" in (result.error or "")


@pytest.mark.asyncio
async def test_generic_hostname_rejects_mutating_arguments() -> None:
    result = await execute_argv(
        "req-5",
        ["hostname", "new-hostname"],
        allowlist={"hostname"},
        timeout_s=2,
        max_output_bytes=1024,
    )
    assert result.rejected is True
    assert "arguments are not permitted" in (result.error or "")


@pytest.mark.asyncio
async def test_generic_execute_rejects_absolute_path_alias(tmp_path) -> None:
    fake = tmp_path / "uptime"
    fake.write_text("#!/bin/sh\necho hijacked\n")
    fake.chmod(0o755)
    result = await execute_argv(
        "req-6",
        [str(fake)],
        allowlist={"uptime"},
        timeout_s=2,
        max_output_bytes=1024,
        exec_search_path=str(tmp_path),
    )
    assert result.rejected is True
    assert "bare name" in (result.error or "")


@pytest.mark.asyncio
async def test_generic_execute_uses_explicit_trusted_search_path(tmp_path) -> None:
    fake = tmp_path / "echo"
    fake.write_text("#!/bin/sh\nprintf 'trusted-test\n'\n")
    fake.chmod(0o755)
    result = await execute_argv(
        "req-7",
        ["echo", "ignored-by-fake"],
        allowlist={"echo"},
        timeout_s=2,
        max_output_bytes=1024,
        exec_search_path=str(tmp_path),
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "trusted-test"

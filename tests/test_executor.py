import sys

import pytest

from remote_mcp_commander.agent.executor import execute_argv


@pytest.mark.asyncio
async def test_rejects_executable_not_in_allowlist() -> None:
    result = await execute_argv(
        "req-1",
        ["definitely-not-allowed"],
        allowlist={"echo"},
        timeout_s=1,
        max_output_bytes=1024,
    )

    assert result.rejected is True
    assert result.returncode is None
    assert "not allowed" in (result.error or "")


@pytest.mark.asyncio
async def test_executes_allowed_binary_without_shell() -> None:
    executable = sys.executable
    result = await execute_argv(
        "req-2",
        [executable, "-c", "print('ok')"],
        allowlist={executable.rsplit('/', 1)[-1]},
        timeout_s=2,
        max_output_bytes=1024,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "ok"
    assert result.rejected is False


@pytest.mark.asyncio
async def test_truncates_output() -> None:
    executable = sys.executable
    result = await execute_argv(
        "req-3",
        [executable, "-c", "print('x' * 100)"],
        allowlist={executable.rsplit('/', 1)[-1]},
        timeout_s=2,
        max_output_bytes=10,
    )

    assert result.returncode == 0
    assert len(result.stdout.encode()) <= 10

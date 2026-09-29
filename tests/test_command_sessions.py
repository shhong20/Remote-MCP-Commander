from pathlib import Path

import pytest

from remote_mcp_commander.agent.session_ops import CommandSessionManager


def make_fake_uptime(path: Path, body: str) -> Path:
    executable = path / "uptime"
    executable.write_text("#!/usr/bin/python3\n" + body)
    executable.chmod(0o755)
    return executable


async def wait_terminal(
    manager: CommandSessionManager,
    session_id: str,
    *,
    attempts: int = 100,
):
    import asyncio

    for _ in range(attempts):
        result = await manager.status("status", session_id)
        if result.state != "running":
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("session did not finish")


def make_manager(**overrides: object) -> CommandSessionManager:
    values: dict[str, object] = {
        "allowlist": {"uptime"},
        "timeout_s": 2.0,
        "max_output_bytes": 1024,
        "max_active": 2,
        "history_limit": 10,
    }
    values.update(overrides)
    return CommandSessionManager(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_command_session_completes_and_returns_bounded_output(tmp_path: Path) -> None:
    make_fake_uptime(tmp_path, "print('session-ok')\n")
    manager = make_manager(exec_search_path=str(tmp_path))
    session_id = "a" * 32

    started = await manager.start("start", session_id, ["uptime"])
    assert started.state == "running"
    assert started.executable == "uptime"

    result = await wait_terminal(manager, session_id)
    assert result.state == "completed"
    assert result.returncode == 0
    assert result.stdout.strip() == "session-ok"
    assert result.finished_at is not None


@pytest.mark.asyncio
async def test_command_session_can_be_cancelled(tmp_path: Path) -> None:
    make_fake_uptime(tmp_path, "import time\ntime.sleep(30)\n")
    manager = make_manager(timeout_s=60.0, exec_search_path=str(tmp_path))
    session_id = "b" * 32
    await manager.start("start", session_id, ["uptime"])

    result = await manager.cancel("cancel", session_id)
    assert result.state == "cancelled"
    assert result.returncode is not None


@pytest.mark.asyncio
async def test_command_session_times_out_and_reaps_process(tmp_path: Path) -> None:
    make_fake_uptime(tmp_path, "import time\ntime.sleep(30)\n")
    manager = make_manager(timeout_s=0.05, exec_search_path=str(tmp_path))
    session_id = "c" * 32
    await manager.start("start", session_id, ["uptime"])

    result = await wait_terminal(manager, session_id)
    assert result.state == "timed_out"
    assert result.returncode is not None
    assert result.error == "session timed out"


@pytest.mark.asyncio
async def test_command_session_enforces_active_limit(tmp_path: Path) -> None:
    make_fake_uptime(tmp_path, "import time\ntime.sleep(30)\n")
    manager = make_manager(timeout_s=60.0, max_active=1, exec_search_path=str(tmp_path))
    first_id = "d" * 32
    second_id = "e" * 32
    await manager.start("start-1", first_id, ["uptime"])

    second = await manager.start("start-2", second_id, ["uptime"])
    assert second.rejected is True
    assert second.error == "too many active command sessions"
    await manager.cancel_all()


@pytest.mark.asyncio
async def test_cancel_all_stops_connection_owned_sessions(tmp_path: Path) -> None:
    make_fake_uptime(tmp_path, "import time\ntime.sleep(30)\n")
    manager = make_manager(timeout_s=60.0, exec_search_path=str(tmp_path))
    first_id = "f" * 32
    second_id = "1" * 32
    await manager.start("start-1", first_id, ["uptime"])
    await manager.start("start-2", second_id, ["uptime"])

    await manager.cancel_all()

    first = await manager.status("status-1", first_id)
    second = await manager.status("status-2", second_id)
    assert first.state == "cancelled"
    assert second.state == "cancelled"


@pytest.mark.asyncio
async def test_command_session_rejects_non_generic_profile(tmp_path: Path) -> None:
    executable = tmp_path / "python"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    manager = make_manager(allowlist={"python"}, exec_search_path=str(tmp_path))

    result = await manager.start("start", "2" * 32, ["python"])
    assert result.rejected is True
    assert "no safe generic profile" in (result.error or "")

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from remote_mcp_commander.agent.executor import execute_argv
from remote_mcp_commander.agent.pty_ops import PtySessionManager
from remote_mcp_commander.agent.session_ops import CommandSessionManager
from remote_mcp_commander.policy import pty_approval_target


@pytest.mark.asyncio
async def test_execute_runs_inside_allowed_working_directory(tmp_path: Path) -> None:
    root = tmp_path / "root"
    work = root / "project"
    work.mkdir(parents=True)
    result = await execute_argv(
        "cwd-exec",
        ["pwd"],
        allowlist={"pwd"},
        timeout_s=2,
        max_output_bytes=1024,
        policy_mode="personal",
        roots=[root.resolve()],
        cwd=str(work),
    )
    assert result.returncode == 0
    assert result.stdout.strip() == str(work.resolve())


@pytest.mark.asyncio
@pytest.mark.parametrize("cwd_kind", ["outside", "relative", "symlink"])
async def test_execute_rejects_unsafe_working_directory(tmp_path: Path, cwd_kind: str) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if cwd_kind == "outside":
        cwd = str(outside)
    elif cwd_kind == "relative":
        cwd = "relative/path"
    else:
        link = root / "escape"
        link.symlink_to(outside, target_is_directory=True)
        cwd = str(link)
    result = await execute_argv(
        "cwd-reject",
        ["pwd"],
        allowlist={"pwd"},
        timeout_s=2,
        max_output_bytes=1024,
        policy_mode="personal",
        roots=[root.resolve()],
        cwd=cwd,
    )
    assert result.rejected is True
    assert "working directory" in (result.error or "")


@pytest.mark.asyncio
async def test_command_session_reports_resolved_working_directory(tmp_path: Path) -> None:
    root = tmp_path / "root"
    work = root / "project"
    work.mkdir(parents=True)
    manager = CommandSessionManager(
        allowlist={"pwd"},
        timeout_s=2,
        max_output_bytes=1024,
        exec_search_path="/usr/bin:/bin",
        policy_mode="personal",
        roots=[root.resolve()],
    )
    session_id = "1" * 32
    started = await manager.start("start", session_id, ["pwd"], cwd=str(work))
    assert started.cwd == str(work.resolve())
    for _ in range(50):
        status = await manager.status("status", session_id)
        if status.state != "running":
            break
        await asyncio.sleep(0.01)
    assert status.state == "completed"
    assert status.stdout.strip() == str(work.resolve())


@pytest.mark.asyncio
async def test_pty_reports_resolved_working_directory(tmp_path: Path) -> None:
    root = tmp_path / "root"
    work = root / "project"
    work.mkdir(parents=True)
    manager = PtySessionManager(
        allowlist={"sh"},
        timeout_s=2,
        max_output_bytes=4096,
        max_input_bytes=1024,
        exec_search_path="/usr/bin:/bin",
        roots=[root.resolve()],
    )
    session_id = "2" * 32
    started = await manager.start(
        "start",
        session_id,
        ["sh", "-c", "pwd"],
        cwd=str(work),
        columns=80,
        rows=24,
    )
    assert started.cwd == str(work.resolve())
    for _ in range(50):
        status = await manager.status("status", session_id)
        if status.state != "running":
            break
        await asyncio.sleep(0.01)
    assert status.state == "completed"
    assert str(work.resolve()) in status.output


def test_pty_approval_target_binds_working_directory() -> None:
    default_target = pty_approval_target(["bash"])
    project_a = pty_approval_target(["bash"], "/home/ubuntu/a")
    project_b = pty_approval_target(["bash"], "/home/ubuntu/b")
    assert default_target != project_a
    assert project_a != project_b
    assert pty_approval_target(["bash"]) == default_target

import asyncio
import os
from pathlib import Path

import pytest

from remote_mcp_commander.agent.pty_ops import PtySessionManager

pytestmark = pytest.mark.skipif(os.name != "posix", reason="PTY sessions require POSIX")


def make_fake_terminal(path: Path, body: str) -> Path:
    executable = path / "terminal"
    executable.write_text("#!/usr/bin/python3\n" + body)
    executable.chmod(0o755)
    return executable


def make_manager(**overrides: object) -> PtySessionManager:
    values: dict[str, object] = {
        "allowlist": {"terminal"},
        "timeout_s": 2.0,
        "max_output_bytes": 4096,
        "max_input_bytes": 64,
        "max_active": 1,
        "history_limit": 10,
    }
    values.update(overrides)
    return PtySessionManager(**values)  # type: ignore[arg-type]


async def wait_for_output(
    manager: PtySessionManager, session_id: str, expected: str, attempts: int = 200
) -> str:
    for _ in range(attempts):
        result = await manager.status("status", session_id)
        if expected in result.output:
            return result.output
        await asyncio.sleep(0.01)
    raise AssertionError(f"PTY output never contained {expected!r}")


async def wait_terminal(manager: PtySessionManager, session_id: str, attempts: int = 200):
    for _ in range(attempts):
        result = await manager.status("status", session_id)
        if result.state != "running":
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("PTY session did not finish")


@pytest.mark.asyncio
async def test_pty_session_is_interactive_and_accepts_input(tmp_path: Path) -> None:
    make_fake_terminal(
        tmp_path,
        "import os\n"
        "print(f'tty={os.isatty(0)}', flush=True)\n"
        "print(f'foreground={os.tcgetpgrp(0) == os.getpgrp()}', flush=True)\n"
        "line = input()\n"
        "print(f'got:{line}', flush=True)\n",
    )
    manager = make_manager(exec_search_path=str(tmp_path))
    session_id = "a" * 32
    started = await manager.start("start", session_id, ["terminal"], columns=100, rows=30)
    assert started.state == "running"
    await wait_for_output(manager, session_id, "tty=True")
    await wait_for_output(manager, session_id, "foreground=True")

    accepted = await manager.input("input", session_id, "hello\n")
    assert accepted.accepted_bytes == 6
    terminal = await wait_terminal(manager, session_id)
    assert terminal.state == "completed"
    assert "got:hello" in terminal.output
    assert terminal.columns == 100
    assert terminal.rows == 30


@pytest.mark.asyncio
async def test_pty_session_resize_updates_terminal(tmp_path: Path) -> None:
    make_fake_terminal(
        tmp_path,
        "import os\n"
        "input()\n"
        "size = os.get_terminal_size(0)\n"
        "print(f'size={size.columns}x{size.lines}', flush=True)\n",
    )
    manager = make_manager(exec_search_path=str(tmp_path))
    session_id = "b" * 32
    await manager.start("start", session_id, ["terminal"], columns=80, rows=24)

    resized = await manager.resize("resize", session_id, columns=132, rows=43)
    assert (resized.columns, resized.rows) == (132, 43)
    await manager.input("input", session_id, "\n")
    terminal = await wait_terminal(manager, session_id)
    assert "size=132x43" in terminal.output


@pytest.mark.asyncio
async def test_pty_session_enforces_separate_allowlist_and_bare_name(tmp_path: Path) -> None:
    make_fake_terminal(tmp_path, "print('nope')\n")
    manager = make_manager(allowlist=set(), exec_search_path=str(tmp_path))
    denied = await manager.start("start", "c" * 32, ["terminal"], columns=80, rows=24)
    assert denied.rejected is True
    assert "not allowed" in (denied.error or "")

    absolute = await manager.start(
        "start", "d" * 32, [str(tmp_path / "terminal")], columns=80, rows=24
    )
    assert absolute.rejected is True
    assert "bare executable name" in (absolute.error or "")


@pytest.mark.asyncio
async def test_pty_input_and_output_are_bounded(tmp_path: Path) -> None:
    make_fake_terminal(
        tmp_path,
        "print('abcdefghij', flush=True)\ninput()\n",
    )
    manager = make_manager(max_output_bytes=5, max_input_bytes=4, exec_search_path=str(tmp_path))
    session_id = "e" * 32
    await manager.start("start", session_id, ["terminal"], columns=80, rows=24)
    await wait_for_output(manager, session_id, "abcde")

    oversized = await manager.input("input", session_id, "12345")
    assert oversized.rejected is True
    snapshot = await manager.status("status", session_id)
    assert snapshot.output == "abcde"
    assert snapshot.output_truncated is True
    await manager.cancel_all()


@pytest.mark.asyncio
async def test_pty_session_can_be_cancelled_and_discarded(tmp_path: Path) -> None:
    make_fake_terminal(tmp_path, "import time\ntime.sleep(30)\n")
    manager = make_manager(timeout_s=60.0, exec_search_path=str(tmp_path))
    session_id = "f" * 32
    await manager.start("start", session_id, ["terminal"], columns=80, rows=24)

    cancelled = await manager.cancel("cancel", session_id)
    assert cancelled.state == "cancelled"
    discarded = await manager.discard("discard", session_id)
    assert discarded.discarded is True

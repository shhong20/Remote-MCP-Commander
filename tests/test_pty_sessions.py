import asyncio
import os
from pathlib import Path

import psutil
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


@pytest.mark.asyncio
async def test_personal_pty_receives_env_override(tmp_path: Path) -> None:
    make_fake_terminal(
        tmp_path,
        "import os\nprint(os.environ.get('DEMO_FLAG', 'missing'), flush=True)\n",
    )
    manager = make_manager(
        exec_search_path=str(tmp_path),
        personal_mode=True,
        child_env={"PATH": str(tmp_path)},
    )
    session_id = "1" * 32
    started = await manager.start(
        "start",
        session_id,
        ["terminal"],
        env_overrides={"DEMO_FLAG": "pty-value"},
        columns=80,
        rows=24,
    )
    assert started.rejected is False
    result = await wait_terminal(manager, session_id)
    assert "pty-value" in result.output


@pytest.mark.asyncio
async def test_hardened_pty_rejects_env_override(tmp_path: Path) -> None:
    make_fake_terminal(tmp_path, "print('should-not-run')\n")
    manager = make_manager(exec_search_path=str(tmp_path))
    result = await manager.start(
        "start",
        "2" * 32,
        ["terminal"],
        env_overrides={"DEMO_FLAG": "blocked"},
        columns=80,
        rows=24,
    )
    assert result.rejected is True
    assert "Personal mode" in (result.error or "")


@pytest.mark.asyncio
async def test_pty_session_listing_filters_running_and_completed(tmp_path: Path) -> None:
    make_fake_terminal(tmp_path, "print('done', flush=True)\n")
    manager = make_manager(exec_search_path=str(tmp_path))
    completed_id = "3" * 32
    await manager.start("done", completed_id, ["terminal"], columns=80, rows=24)
    await wait_terminal(manager, completed_id)

    make_fake_terminal(tmp_path, "import time\ntime.sleep(30)\n")
    running_id = "4" * 32
    await manager.start("running", running_id, ["terminal"], columns=80, rows=24)

    active = await manager.list_infos(include_completed=False)
    assert [item.session_id for item in active] == [running_id]
    assert active[0].kind == "pty"
    assert active[0].state == "running"

    all_infos = await manager.list_infos(include_completed=True)
    by_id = {item.session_id: item for item in all_infos}
    assert set(by_id) == {completed_id, running_id}
    assert by_id[completed_id].state == "completed"
    assert by_id[completed_id].output_chars >= len("done")
    await manager.cancel_all()


@pytest.mark.asyncio
async def test_pty_output_lines_supports_range_and_tail(tmp_path: Path) -> None:
    make_fake_terminal(
        tmp_path,
        "import sys\nsys.stdout.write('a\\nb\\nc')\nsys.stdout.flush()\n",
    )
    manager = make_manager(exec_search_path=str(tmp_path))
    session_id = "5" * 32
    await manager.start("start", session_id, ["terminal"], columns=80, rows=24)
    terminal = await wait_terminal(manager, session_id)
    assert terminal.state == "completed"

    middle = await manager.output_lines("mid", session_id, offset=1, max_lines=1)
    assert middle.content.replace("\r", "") == "b\n"
    assert middle.total_lines == 3
    assert middle.eof is False

    tail = await manager.output_lines("tail", session_id, offset=-2, max_lines=1)
    assert tail.content.replace("\r", "") == "b\nc"
    assert tail.eof is True


@pytest.mark.asyncio
async def test_running_pty_output_lines_hides_partial_last_line(tmp_path: Path) -> None:
    make_fake_terminal(
        tmp_path,
        "import sys,time\n"
        "sys.stdout.write('ready\\npartial\\r')\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n",
    )
    manager = make_manager(timeout_s=60.0, exec_search_path=str(tmp_path))
    session_id = "6" * 32
    await manager.start("start", session_id, ["terminal"], columns=80, rows=24)
    await wait_for_output(manager, session_id, "partial")

    page = await manager.output_lines("page", session_id, offset=0, max_lines=10)
    assert page.content.replace("\r", "") == "ready\n"
    assert page.total_lines == 1
    assert page.pending_partial is True
    assert page.eof is False
    await manager.cancel("cancel", session_id)


@pytest.mark.asyncio
async def test_pty_line_long_poll_wakes_on_stable_line(tmp_path: Path) -> None:
    make_fake_terminal(
        tmp_path,
        "import sys,time\n"
        "sys.stdout.write('partial')\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.08)\n"
        "sys.stdout.write('-done\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n",
    )
    manager = make_manager(timeout_s=60.0, exec_search_path=str(tmp_path))
    session_id = "7" * 32
    await manager.start("start", session_id, ["terminal"], columns=80, rows=24)

    page = await manager.output_lines(
        "wait", session_id, offset=0, max_lines=10, wait_ms=1000
    )
    assert page.content.replace("\r", "") == "partial-done\n"
    assert page.wait_timed_out is False
    assert 0 < page.waited_ms < 1000
    await manager.cancel("cancel", session_id)

@pytest.mark.asyncio
async def test_pty_line_long_poll_times_out_on_partial_line(tmp_path: Path) -> None:
    make_fake_terminal(
        tmp_path,
        "import sys,time\n"
        "sys.stdout.write('partial')\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n",
    )
    manager = make_manager(timeout_s=60.0, exec_search_path=str(tmp_path))
    session_id = "8" * 32
    await manager.start("start", session_id, ["terminal"], columns=80, rows=24)
    await wait_for_output(manager, session_id, "partial")

    page = await manager.output_lines(
        "wait", session_id, offset=0, max_lines=10, wait_ms=80
    )
    assert page.content == ""
    assert page.pending_partial is True
    assert page.wait_timed_out is True
    assert page.waited_ms >= 50
    await manager.cancel("cancel", session_id)

@pytest.mark.asyncio
async def test_pty_session_exposes_stable_process_identity(tmp_path: Path) -> None:
    make_fake_terminal(tmp_path, "import time\ntime.sleep(0.2)\n")
    manager = make_manager(exec_search_path=str(tmp_path))
    session_id = "b2" * 16

    started = await manager.start("start", session_id, ["terminal"], columns=80, rows=24)
    assert started.pid is not None
    assert started.create_time_ms == round(psutil.Process(started.pid).create_time() * 1000)

    active = await manager.list_infos(include_completed=False)
    assert len(active) == 1
    assert (active[0].pid, active[0].create_time_ms) == (
        started.pid,
        started.create_time_ms,
    )

    terminal = await wait_terminal(manager, session_id)
    assert terminal.state == "completed"
    retained = await manager.list_infos(include_completed=True)
    assert (retained[0].pid, retained[0].create_time_ms) == (
        started.pid,
        started.create_time_ms,
    )


@pytest.mark.asyncio
async def test_pty_session_signal_rechecks_identity_and_targets_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_fake_terminal(tmp_path, "import time\ntime.sleep(30)\n")
    manager = make_manager(timeout_s=60.0, exec_search_path=str(tmp_path))
    session_id = "f2" * 16
    started = await manager.start(
        "start", session_id, ["terminal"], columns=80, rows=24
    )
    assert started.pid is not None
    assert started.create_time_ms is not None

    seen: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "remote_mcp_commander.agent.pty_ops.os.killpg",
        lambda pid, sig: seen.append((pid, sig)),
    )
    result = await manager.signal_session("signal", session_id, "hup")
    assert result.signal_sent is True
    assert result.pid == started.pid
    assert result.create_time_ms == started.create_time_ms
    assert seen and seen[0][0] == started.pid
    monkeypatch.undo()
    await manager.cancel("cancel", session_id)

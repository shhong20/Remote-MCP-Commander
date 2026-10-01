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


@pytest.mark.asyncio
async def test_command_output_streams_incrementally_with_character_cursors(tmp_path: Path) -> None:
    import asyncio

    make_fake_uptime(
        tmp_path,
        "import time\nprint('가', flush=True)\ntime.sleep(0.2)\nprint('나', flush=True)\n",
    )
    manager = make_manager(timeout_s=2.0, exec_search_path=str(tmp_path))
    session_id = "3" * 32
    await manager.start("start", session_id, ["uptime"])

    first = None
    for _ in range(100):
        candidate = await manager.output(
            "out-1", session_id, stdout_offset=0, stderr_offset=0, max_chars=1
        )
        if candidate.stdout:
            first = candidate
            break
        await asyncio.sleep(0.01)
    assert first is not None
    assert first.stdout == "가"
    assert first.next_stdout_offset == 1

    terminal = await wait_terminal(manager, session_id)
    assert terminal.state == "completed"
    rest = await manager.output(
        "out-2",
        session_id,
        stdout_offset=first.next_stdout_offset,
        stderr_offset=0,
        max_chars=32,
    )
    assert "나" in rest.stdout
    assert rest.next_stdout_offset > first.next_stdout_offset


@pytest.mark.asyncio
async def test_command_output_marks_retention_truncation(tmp_path: Path) -> None:
    make_fake_uptime(tmp_path, "print('abcdefghij')\n")
    manager = make_manager(max_output_bytes=5, exec_search_path=str(tmp_path))
    session_id = "4" * 32
    await manager.start("start", session_id, ["uptime"])

    result = await wait_terminal(manager, session_id)
    assert result.stdout == "abcde"
    assert result.stdout_truncated is True

    output = await manager.output("out", session_id, stdout_offset=0, stderr_offset=0, max_chars=32)
    assert output.stdout == "abcde"
    assert output.stdout_truncated is True


@pytest.mark.asyncio
async def test_completed_session_can_be_discarded_but_running_session_cannot(
    tmp_path: Path,
) -> None:
    make_fake_uptime(tmp_path, "print('done')\n")
    manager = make_manager(exec_search_path=str(tmp_path))
    completed_id = "5" * 32
    await manager.start("start", completed_id, ["uptime"])
    await wait_terminal(manager, completed_id)

    discarded = await manager.discard("discard", completed_id)
    assert discarded.discarded is True
    missing = await manager.status("status", completed_id)
    assert missing.rejected is True

    make_fake_uptime(tmp_path, "import time\ntime.sleep(30)\n")
    running_id = "6" * 32
    await manager.start("start-running", running_id, ["uptime"])
    rejected = await manager.discard("discard-running", running_id)
    assert rejected.rejected is True
    assert rejected.error == "running session cannot be discarded"
    await manager.cancel_all()


@pytest.mark.asyncio
async def test_personal_command_session_receives_env_override(tmp_path: Path) -> None:
    make_fake_uptime(
        tmp_path,
        "import os\nprint(os.environ.get('DEMO_FLAG', 'missing'))\n",
    )
    manager = make_manager(
        exec_search_path=str(tmp_path),
        policy_mode="personal",
        child_env={"PATH": str(tmp_path)},
    )
    session_id = "7" * 32
    started = await manager.start(
        "start",
        session_id,
        ["uptime"],
        env_overrides={"DEMO_FLAG": "session-value"},
    )
    assert started.rejected is False
    result = await wait_terminal(manager, session_id)
    assert result.stdout.strip() == "session-value"


@pytest.mark.asyncio
async def test_hardened_command_session_rejects_env_override(tmp_path: Path) -> None:
    make_fake_uptime(tmp_path, "print('should-not-run')\n")
    manager = make_manager(exec_search_path=str(tmp_path))
    result = await manager.start(
        "start",
        "8" * 32,
        ["uptime"],
        env_overrides={"DEMO_FLAG": "blocked"},
    )
    assert result.rejected is True
    assert "Personal mode" in (result.error or "")


@pytest.mark.asyncio
async def test_command_session_listing_filters_running_and_completed(tmp_path: Path) -> None:
    make_fake_uptime(tmp_path, "print('done')\n")
    manager = make_manager(exec_search_path=str(tmp_path))
    completed_id = "9" * 32
    await manager.start("start-done", completed_id, ["uptime"])
    await wait_terminal(manager, completed_id)

    make_fake_uptime(tmp_path, "import time\ntime.sleep(30)\n")
    running_id = "a" * 32
    await manager.start("start-running", running_id, ["uptime"])

    active = await manager.list_infos(include_completed=False)
    assert [item.session_id for item in active] == [running_id]
    assert active[0].kind == "command"
    assert active[0].state == "running"

    all_infos = await manager.list_infos(include_completed=True)
    by_id = {item.session_id: item for item in all_infos}
    assert set(by_id) == {completed_id, running_id}
    assert by_id[completed_id].state == "completed"
    assert by_id[completed_id].output_chars == len("done\n")
    await manager.cancel_all()


@pytest.mark.asyncio
async def test_command_session_accepts_stdin_until_explicit_eof(tmp_path: Path) -> None:
    make_fake_uptime(
        tmp_path,
        "import sys\ndata = sys.stdin.read()\nprint('got:' + data, end='')\n",
    )
    manager = make_manager(timeout_s=2.0, exec_search_path=str(tmp_path))
    session_id = "5" * 32
    started = await manager.start("start", session_id, ["uptime"])
    assert started.state == "running"

    first = await manager.write_input("input-1", session_id, "hello\n")
    second = await manager.write_input("input-2", session_id, "world\n")
    assert first.accepted_bytes == len(b"hello\n")
    assert second.accepted_bytes == len(b"world\n")

    closed = await manager.close_stdin("close", session_id)
    assert closed.closed is True
    result = await wait_terminal(manager, session_id)
    assert result.state == "completed"
    assert result.stdout == "got:hello\nworld\n"

    after_close = await manager.write_input("late", session_id, "nope")
    assert after_close.rejected is True
    assert "not writable" in (after_close.error or "")


@pytest.mark.asyncio
async def test_command_session_input_byte_limit_and_close_idempotency(tmp_path: Path) -> None:
    make_fake_uptime(tmp_path, "import sys\nsys.stdin.read()\n")
    manager = make_manager(
        timeout_s=2.0, max_input_bytes=4, exec_search_path=str(tmp_path)
    )
    session_id = "6" * 32
    await manager.start("start", session_id, ["uptime"])

    rejected = await manager.write_input("large", session_id, "12345")
    assert rejected.rejected is True
    assert "size limit" in (rejected.error or "")
    first = await manager.close_stdin("close-1", session_id)
    second = await manager.close_stdin("close-2", session_id)
    assert first.closed is True
    assert second.closed is True
    await wait_terminal(manager, session_id)


@pytest.mark.asyncio
async def test_timeout_uses_process_group_termination_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_fake_uptime(tmp_path, "import time\ntime.sleep(30)\n")
    manager = make_manager(timeout_s=0.05, exec_search_path=str(tmp_path))
    called = 0
    original = manager._terminate_process

    async def tracked(session) -> None:
        nonlocal called
        called += 1
        await original(session)

    monkeypatch.setattr(manager, "_terminate_process", tracked)
    session_id = "7" * 32
    await manager.start("start", session_id, ["uptime"])
    result = await wait_terminal(manager, session_id)
    assert result.state == "timed_out"
    assert called == 1


@pytest.mark.asyncio
async def test_cancel_uses_process_group_termination_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_fake_uptime(tmp_path, "import time\ntime.sleep(30)\n")
    manager = make_manager(timeout_s=60.0, exec_search_path=str(tmp_path))
    called = 0
    original = manager._terminate_process

    async def tracked(session) -> None:
        nonlocal called
        called += 1
        await original(session)

    monkeypatch.setattr(manager, "_terminate_process", tracked)
    session_id = "8" * 32
    await manager.start("start", session_id, ["uptime"])
    result = await manager.cancel("cancel", session_id)
    assert result.state == "cancelled"
    assert called == 1


@pytest.mark.asyncio
async def test_command_session_timeout_override_is_applied(tmp_path: Path) -> None:
    make_fake_uptime(tmp_path, "import time\ntime.sleep(0.1)\nprint('done')\n")
    manager = make_manager(timeout_s=0.05, exec_search_path=str(tmp_path))
    session_id = "9" * 32
    started = await manager.start(
        "start", session_id, ["uptime"], timeout_s=1.0
    )
    assert started.timeout_s == 1.0
    result = await wait_terminal(manager, session_id)
    assert result.state == "completed"
    assert result.timeout_s == 1.0
    assert result.stdout.strip() == "done"


@pytest.mark.asyncio
async def test_command_output_lines_supports_range_tail_and_stderr(tmp_path: Path) -> None:
    body = (
        "import sys\n"
        "sys.stdout.write('a\\nb\\nc')\n"
        "sys.stderr.write('e1\\ne2\\n')\n"
    )
    make_fake_uptime(tmp_path, body)
    manager = make_manager(exec_search_path=str(tmp_path))
    session_id = "a1" * 16
    await manager.start("start", session_id, ["uptime"])
    result = await wait_terminal(manager, session_id)
    assert result.state == "completed"

    middle = await manager.output_lines(
        "mid", session_id, stream="stdout", offset=1, max_lines=1
    )
    assert middle.content == "b\n"
    assert (middle.total_lines, middle.start_line, middle.next_line) == (3, 1, 2)
    assert middle.eof is False

    tail = await manager.output_lines(
        "tail", session_id, stream="stdout", offset=-2, max_lines=1
    )
    assert tail.content == "b\nc"
    assert tail.eof is True
    stderr = await manager.output_lines(
        "err", session_id, stream="stderr", offset=0, max_lines=10
    )
    assert stderr.content == "e1\ne2\n"
    assert stderr.eof is True


@pytest.mark.asyncio
async def test_running_command_output_lines_hides_partial_last_line(tmp_path: Path) -> None:
    body = (
        "import sys,time\n"
        "sys.stdout.write('ready\\npartial')\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    make_fake_uptime(tmp_path, body)
    manager = make_manager(timeout_s=60.0, exec_search_path=str(tmp_path))
    session_id = "a2" * 16
    await manager.start("start", session_id, ["uptime"])

    import asyncio
    await asyncio.sleep(0.05)
    page = await manager.output_lines(
        "page", session_id, stream="stdout", offset=0, max_lines=10
    )
    assert page.content == "ready\n"
    assert page.total_lines == 1
    assert page.pending_partial is True
    assert page.eof is False
    await manager.cancel("cancel", session_id)

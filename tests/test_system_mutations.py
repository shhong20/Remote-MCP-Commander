from pathlib import Path

import pytest

from remote_mcp_commander.agent import system_ops


class FakeProcess:
    def __init__(self, create_time: float) -> None:
        self._create_time = create_time
        self.terminated = False

    def create_time(self) -> float:
        return self._create_time

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float) -> None:
        assert timeout == 2


@pytest.mark.asyncio
async def test_terminate_rechecks_process_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(123.456)
    monkeypatch.setattr(system_ops.psutil, "Process", lambda pid: process)
    result = await system_ops.terminate_process("term-1", 99, 999999)
    assert result.rejected is True
    assert result.signal_sent is False
    assert process.terminated is False


@pytest.mark.asyncio
async def test_terminate_sends_sigterm_for_matching_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess(123.456)
    monkeypatch.setattr(system_ops.psutil, "Process", lambda pid: process)
    result = await system_ops.terminate_process("term-2", 99, 123456)
    assert result.rejected is False
    assert result.signal_sent is True
    assert result.exited is True
    assert process.terminated is True


@pytest.mark.asyncio
async def test_service_action_rejects_shell_like_unit() -> None:
    result = await system_ops.service_action(
        "action-bad",
        "demo.service;touch-/tmp/x",
        "restart",
        timeout_s=1,
    )
    assert result.rejected is True
    assert result.error == "invalid service unit name"


@pytest.mark.asyncio
async def test_service_action_uses_fixed_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args_file = tmp_path / "args.txt"
    fake = tmp_path / "systemctl"
    fake.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$ARGS_FILE"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("ARGS_FILE", str(args_file))
    monkeypatch.setattr(system_ops.shutil, "which", lambda name: str(fake))

    result = await system_ops.service_action(
        "action-1",
        "demo.service",
        "restart",
        timeout_s=2,
    )
    assert result.returncode == 0
    assert args_file.read_text().splitlines() == ["restart", "demo.service", "--no-pager"]

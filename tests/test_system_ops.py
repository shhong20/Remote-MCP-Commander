import os
from pathlib import Path

import psutil
import pytest

from remote_mcp_commander.agent import system_ops


@pytest.mark.asyncio
async def test_list_processes_is_bounded_and_omits_sensitive_command_line() -> None:
    result = await system_ops.list_processes("proc-1", 5)

    assert result.rejected is False
    assert len(result.processes) <= 5
    for process in result.processes:
        payload = process.model_dump()
        assert "cmdline" not in payload
        assert "environ" not in payload
        assert process.pid > 0
        assert process.create_time_ms > 0


@pytest.mark.asyncio
async def test_service_status_rejects_shell_like_unit_names() -> None:
    result = await system_ops.service_status(
        "svc-bad",
        "ssh.service;touch-/tmp/pwned",
        timeout_s=1,
    )
    assert result.rejected is True
    assert result.error == "invalid service unit name"


@pytest.mark.asyncio
async def test_service_status_parses_fixed_systemctl_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = tmp_path / "systemctl"
    fake.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' "
        "'Id=demo.service' "
        "'Description=Demo Service' "
        "'LoadState=loaded' "
        "'ActiveState=active' "
        "'SubState=running' "
        "'UnitFileState=enabled'\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setattr(system_ops.shutil, "which", lambda name: str(fake))

    result = await system_ops.service_status("svc-1", "demo.service", timeout_s=2)
    assert result.rejected is False
    assert result.id == "demo.service"
    assert result.active_state == "active"
    assert result.sub_state == "running"


@pytest.mark.asyncio
async def test_process_info_reads_one_pid_without_sensitive_fields() -> None:
    pid = os.getpid()
    result = await system_ops.process_info("proc-info", pid)

    assert result.rejected is False
    assert result.process is not None
    assert result.process.pid == pid
    assert result.process.create_time_ms > 0
    payload = result.process.model_dump()
    assert "cmdline" not in payload
    assert "environ" not in payload


@pytest.mark.asyncio
async def test_process_info_rejects_missing_process(monkeypatch: pytest.MonkeyPatch) -> None:
    pid = 999999

    def missing_process(requested_pid: int):
        raise psutil.NoSuchProcess(requested_pid)

    monkeypatch.setattr(system_ops.psutil, "Process", missing_process)
    result = await system_ops.process_info("proc-missing", pid)

    assert result.rejected is True
    assert result.process is None
    assert result.error == "process no longer exists"

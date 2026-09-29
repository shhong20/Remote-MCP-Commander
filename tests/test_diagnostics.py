from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from remote_mcp_commander.agent import diagnostics


@pytest.mark.asyncio
async def test_system_health_returns_bounded_host_metrics() -> None:
    result = await diagnostics.system_health("health-1")

    assert result.rejected is False
    assert result.uptime_seconds is not None and result.uptime_seconds >= 0
    assert result.cpu_count is None or result.cpu_count > 0
    assert result.memory_total is not None and result.memory_total > 0
    assert result.memory_available is not None and result.memory_available >= 0
    assert result.disk_total is not None and result.disk_total > 0


@pytest.mark.asyncio
async def test_port_lookup_returns_listener_without_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = SimpleNamespace(
        status=psutil.CONN_LISTEN,
        laddr=SimpleNamespace(ip="127.0.0.1", port=8005),
        pid=123,
    )
    monkeypatch.setattr(diagnostics.psutil, "net_connections", lambda kind: [listener])
    monkeypatch.setattr(
        diagnostics.psutil,
        "Process",
        lambda pid: SimpleNamespace(name=lambda: "uvicorn"),
    )

    result = await diagnostics.lookup_port("port-1", 8005)
    assert result.rejected is False
    assert result.truncated is False
    assert len(result.listeners) == 1
    item = result.listeners[0]
    assert item.local_address == "127.0.0.1:8005"
    assert item.pid == 123
    assert item.process_name == "uvicorn"
    assert "cmdline" not in item.model_dump()


@pytest.mark.asyncio
async def test_service_logs_rejects_shell_like_unit_name() -> None:
    result = await diagnostics.service_logs(
        "logs-bad",
        "demo.service;touch-/tmp/pwned",
        10,
        timeout_s=1,
        max_output_bytes=1024,
    )
    assert result.rejected is True
    assert result.error == "invalid service unit name"


@pytest.mark.asyncio
async def test_service_logs_uses_fixed_journalctl_args_and_bounds_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args_file = tmp_path / "args.txt"
    fake = tmp_path / "journalctl"
    fake.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{args_file}'\nprintf 'abcdef\\n'\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name, path=None: str(fake))

    result = await diagnostics.service_logs(
        "logs-1",
        "demo.service",
        25,
        timeout_s=2,
        max_output_bytes=4,
    )
    assert result.returncode == 0
    assert result.text == "abcd"
    assert result.truncated is True

    args = args_file.read_text(encoding="utf-8").splitlines()
    assert args == [
        "--unit=demo.service",
        "--lines=25",
        "--no-pager",
        "--output=short-iso",
    ]

from __future__ import annotations

import asyncio
import os
import shutil
from datetime import UTC, datetime

import psutil

from remote_mcp_commander.agent.system_ops import SERVICE_UNIT_RE
from remote_mcp_commander.protocol import (
    PortListener,
    PortLookupResult,
    ServiceLogsResult,
    SystemHealthResult,
)

TRUSTED_SYSTEM_PATH = "/usr/bin:/bin"
_MAX_PORT_LISTENERS = 50


def _system_health_sync(request_id: str) -> SystemHealthResult:
    try:
        boot_timestamp = psutil.boot_time()
        now = datetime.now(UTC)
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk = psutil.disk_usage("/")
        try:
            load_1m, load_5m, load_15m = os.getloadavg()
        except (AttributeError, OSError):
            load_1m = load_5m = load_15m = None
        return SystemHealthResult(
            request_id=request_id,
            boot_time=datetime.fromtimestamp(boot_timestamp, tz=UTC),
            uptime_seconds=max(0, round(now.timestamp() - boot_timestamp)),
            cpu_count=psutil.cpu_count(logical=True),
            cpu_percent=psutil.cpu_percent(interval=0.1),
            load_1m=load_1m,
            load_5m=load_5m,
            load_15m=load_15m,
            memory_total=memory.total,
            memory_available=memory.available,
            memory_percent=memory.percent,
            swap_total=swap.total,
            swap_used=swap.used,
            swap_percent=swap.percent,
            disk_total=disk.total,
            disk_free=disk.free,
            disk_percent=disk.percent,
        )
    except (OSError, RuntimeError) as exc:
        return SystemHealthResult(request_id=request_id, rejected=True, error=str(exc))


async def system_health(request_id: str) -> SystemHealthResult:
    return await asyncio.to_thread(_system_health_sync, request_id)


def _format_local_address(address: object) -> str:
    if not address:
        return ""
    host = getattr(address, "ip", None)
    port = getattr(address, "port", None)
    if host is None or port is None:
        host, port = address  # type: ignore[misc]
    host_text = str(host)
    return f"[{host_text}]:{int(port)}" if ":" in host_text else f"{host_text}:{int(port)}"


def _lookup_port_sync(request_id: str, port: int) -> PortLookupResult:
    try:
        connections = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, OSError) as exc:
        return PortLookupResult(
            request_id=request_id,
            port=port,
            rejected=True,
            error=f"network connection inspection failed: {type(exc).__name__}",
        )

    listeners: list[PortListener] = []
    for connection in connections:
        if connection.status != psutil.CONN_LISTEN or not connection.laddr:
            continue
        local_port = int(connection.laddr.port)
        if local_port != port:
            continue
        process_name = None
        if connection.pid is not None:
            try:
                process_name = psutil.Process(connection.pid).name()
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
        listeners.append(
            PortListener(
                local_address=_format_local_address(connection.laddr),
                pid=connection.pid,
                process_name=process_name,
            )
        )

    listeners.sort(key=lambda item: (item.pid is None, item.pid or 0, item.local_address))
    return PortLookupResult(
        request_id=request_id,
        port=port,
        listeners=listeners[:_MAX_PORT_LISTENERS],
        truncated=len(listeners) > _MAX_PORT_LISTENERS,
    )


async def lookup_port(request_id: str, port: int) -> PortLookupResult:
    return await asyncio.to_thread(_lookup_port_sync, request_id, port)


async def service_logs(
    request_id: str,
    unit: str,
    lines: int,
    *,
    timeout_s: float,
    max_output_bytes: int,
) -> ServiceLogsResult:
    if not SERVICE_UNIT_RE.fullmatch(unit):
        return ServiceLogsResult(
            request_id=request_id,
            unit=unit,
            rejected=True,
            error="invalid service unit name",
        )
    journalctl = shutil.which("journalctl", path=TRUSTED_SYSTEM_PATH)
    if journalctl is None:
        return ServiceLogsResult(
            request_id=request_id,
            unit=unit,
            rejected=True,
            error="journalctl is not available",
        )

    process = await asyncio.create_subprocess_exec(
        journalctl,
        f"--unit={unit}",
        f"--lines={lines}",
        "--no-pager",
        "--output=short-iso",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"PATH": TRUSTED_SYSTEM_PATH, "SYSTEMD_PAGER": "cat", "PAGER": "cat"},
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except TimeoutError:
        process.kill()
        await process.wait()
        return ServiceLogsResult(
            request_id=request_id,
            unit=unit,
            error="journalctl query timed out",
        )

    truncated = len(stdout) > max_output_bytes
    text = stdout[:max_output_bytes].decode("utf-8", errors="replace")
    error = None
    if process.returncode not in (0, None):
        error = stderr.decode("utf-8", errors="replace")[:512].strip() or "journalctl query failed"
    return ServiceLogsResult(
        request_id=request_id,
        unit=unit,
        text=text,
        returncode=process.returncode,
        truncated=truncated,
        error=error,
    )

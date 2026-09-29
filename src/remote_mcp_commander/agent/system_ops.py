from __future__ import annotations

import asyncio
import re
import shutil

import psutil

from remote_mcp_commander.protocol import (
    ProcessInfo,
    ProcessListResult,
    ServiceStatusResult,
)

SERVICE_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@:-]{1,256}$")


def _list_processes_sync(request_id: str, limit: int) -> ProcessListResult:
    processes: list[ProcessInfo] = []
    for process in psutil.process_iter(attrs=["pid", "name", "username", "status", "memory_info"]):
        try:
            info = process.info
            memory = info.get("memory_info")
            processes.append(
                ProcessInfo(
                    pid=int(info["pid"]),
                    name=str(info.get("name") or ""),
                    username=info.get("username"),
                    status=info.get("status"),
                    memory_rss=int(memory.rss) if memory is not None else None,
                )
            )
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue

    processes.sort(key=lambda item: item.pid)
    truncated = len(processes) > limit
    return ProcessListResult(
        request_id=request_id,
        processes=processes[:limit],
        truncated=truncated,
    )


async def list_processes(request_id: str, limit: int) -> ProcessListResult:
    return await asyncio.to_thread(_list_processes_sync, request_id, limit)


async def service_status(request_id: str, unit: str, timeout_s: float) -> ServiceStatusResult:
    if not SERVICE_UNIT_RE.fullmatch(unit):
        return ServiceStatusResult(
            request_id=request_id,
            unit=unit,
            rejected=True,
            error="invalid service unit name",
        )

    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return ServiceStatusResult(
            request_id=request_id,
            unit=unit,
            rejected=True,
            error="systemctl is not available",
        )

    properties = "Id,LoadState,ActiveState,SubState,UnitFileState,Description"
    process = await asyncio.create_subprocess_exec(
        systemctl,
        "show",
        unit,
        "--no-pager",
        f"--property={properties}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except TimeoutError:
        process.kill()
        await process.wait()
        return ServiceStatusResult(
            request_id=request_id,
            unit=unit,
            error="systemctl query timed out",
        )

    values: dict[str, str] = {}
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value

    error = None
    if process.returncode not in (0, None) and not values:
        error = stderr.decode("utf-8", errors="replace")[:512].strip() or "systemctl query failed"

    return ServiceStatusResult(
        request_id=request_id,
        unit=unit,
        id=values.get("Id"),
        description=values.get("Description"),
        load_state=values.get("LoadState"),
        active_state=values.get("ActiveState"),
        sub_state=values.get("SubState"),
        unit_file_state=values.get("UnitFileState"),
        returncode=process.returncode,
        error=error,
    )

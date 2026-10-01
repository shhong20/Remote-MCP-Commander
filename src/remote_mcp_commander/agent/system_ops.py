from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal as signal_module

import psutil

from remote_mcp_commander.protocol import (
    ProcessInfo,
    ProcessListResult,
    ProcessSignal,
    ProcessSignalResult,
    ProcessTerminateResult,
    ServiceAction,
    ServiceActionResult,
    ServiceStatusResult,
)

SERVICE_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@:-]{1,256}$")


def process_create_time_ms(pid: int) -> int | None:
    try:
        return round(psutil.Process(pid).create_time() * 1000)
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return None


def _list_processes_sync(request_id: str, limit: int) -> ProcessListResult:
    processes: list[ProcessInfo] = []
    for process in psutil.process_iter(attrs=["pid", "name", "username", "status", "memory_info"]):
        try:
            info = process.info
            memory = info.get("memory_info")
            create_time_ms = round(process.create_time() * 1000)
            processes.append(
                ProcessInfo(
                    pid=int(info["pid"]),
                    create_time_ms=create_time_ms,
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


def _terminate_process_sync(
    request_id: str,
    pid: int,
    expected_create_time_ms: int,
) -> ProcessTerminateResult:
    if pid == os.getpid():
        return ProcessTerminateResult(
            request_id=request_id,
            pid=pid,
            rejected=True,
            error="refusing to terminate the Agent process",
        )
    try:
        process = psutil.Process(pid)
        actual_create_time_ms = round(process.create_time() * 1000)
        if actual_create_time_ms != expected_create_time_ms:
            return ProcessTerminateResult(
                request_id=request_id,
                pid=pid,
                rejected=True,
                error="process identity changed since approval",
            )
        process.terminate()
        signal_sent = True
        try:
            process.wait(timeout=2)
            exited = True
        except psutil.TimeoutExpired:
            exited = False
        return ProcessTerminateResult(
            request_id=request_id,
            pid=pid,
            signal_sent=signal_sent,
            exited=exited,
        )
    except psutil.NoSuchProcess:
        return ProcessTerminateResult(
            request_id=request_id,
            pid=pid,
            rejected=True,
            error="process no longer exists",
        )
    except psutil.AccessDenied:
        return ProcessTerminateResult(
            request_id=request_id,
            pid=pid,
            rejected=True,
            error="permission denied",
        )


async def terminate_process(
    request_id: str,
    pid: int,
    expected_create_time_ms: int,
) -> ProcessTerminateResult:
    return await asyncio.to_thread(
        _terminate_process_sync,
        request_id,
        pid,
        expected_create_time_ms,
    )


def _signal_process_sync(
    request_id: str,
    pid: int,
    expected_create_time_ms: int,
    requested_signal: ProcessSignal,
) -> ProcessSignalResult:
    if pid == os.getpid():
        return ProcessSignalResult(
            request_id=request_id,
            pid=pid,
            signal=requested_signal,
            rejected=True,
            error="refusing to signal the Agent process",
        )
    signal_map = {
        "term": signal_module.SIGTERM,
        "kill": signal_module.SIGKILL,
        "int": signal_module.SIGINT,
        "hup": getattr(signal_module, "SIGHUP", None),
    }
    signal_value = signal_map[requested_signal]
    if signal_value is None:
        return ProcessSignalResult(
            request_id=request_id,
            pid=pid,
            signal=requested_signal,
            rejected=True,
            error=f"signal is unavailable on this platform: {requested_signal}",
        )
    try:
        process = psutil.Process(pid)
        actual_create_time_ms = round(process.create_time() * 1000)
        if actual_create_time_ms != expected_create_time_ms:
            return ProcessSignalResult(
                request_id=request_id,
                pid=pid,
                signal=requested_signal,
                rejected=True,
                error="process identity changed since inspection",
            )
        process.send_signal(signal_value)
        try:
            process.wait(timeout=2)
            exited = True
        except psutil.TimeoutExpired:
            exited = False
        return ProcessSignalResult(
            request_id=request_id,
            pid=pid,
            signal=requested_signal,
            signal_sent=True,
            exited=exited,
        )
    except psutil.NoSuchProcess:
        return ProcessSignalResult(
            request_id=request_id,
            pid=pid,
            signal=requested_signal,
            rejected=True,
            error="process no longer exists",
        )
    except psutil.AccessDenied:
        return ProcessSignalResult(
            request_id=request_id,
            pid=pid,
            signal=requested_signal,
            rejected=True,
            error="permission denied",
        )


async def signal_process(
    request_id: str,
    pid: int,
    expected_create_time_ms: int,
    requested_signal: ProcessSignal,
) -> ProcessSignalResult:
    return await asyncio.to_thread(
        _signal_process_sync,
        request_id,
        pid,
        expected_create_time_ms,
        requested_signal,
    )


async def service_action(
    request_id: str,
    unit: str,
    action: ServiceAction,
    timeout_s: float,
) -> ServiceActionResult:
    if not SERVICE_UNIT_RE.fullmatch(unit):
        return ServiceActionResult(
            request_id=request_id,
            unit=unit,
            action=action,
            rejected=True,
            error="invalid service unit name",
        )

    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return ServiceActionResult(
            request_id=request_id,
            unit=unit,
            action=action,
            rejected=True,
            error="systemctl is not available",
        )

    process = await asyncio.create_subprocess_exec(
        systemctl,
        action,
        unit,
        "--no-pager",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except TimeoutError:
        process.kill()
        await process.wait()
        return ServiceActionResult(
            request_id=request_id,
            unit=unit,
            action=action,
            error="systemctl action timed out",
        )

    error = None
    if process.returncode not in (0, None):
        error = stderr.decode("utf-8", errors="replace")[:512].strip() or "systemctl action failed"
    return ServiceActionResult(
        request_id=request_id,
        unit=unit,
        action=action,
        returncode=process.returncode,
        error=error,
    )

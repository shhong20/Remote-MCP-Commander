from __future__ import annotations

import os
import shutil
from pathlib import Path

from remote_mcp_commander.agent.diagnostics import TRUSTED_SYSTEM_PATH
from remote_mcp_commander.agent.git_ops import TRUSTED_GIT_PATH
from remote_mcp_commander.agent.pty_ops import resolve_pty_executable
from remote_mcp_commander.config import Settings
from remote_mcp_commander.policy import (
    generic_executables_for_mode,
    resolve_generic_executable,
)

BASE_CAPABILITIES = {
    "device.ping",
    "diagnostics.system_health",
    "diagnostics.port_lookup",
    "process.list",
    "process.terminate",
}


def detect_capabilities(settings: Settings, roots: list[Path]) -> list[str]:
    capabilities = set(BASE_CAPABILITIES)

    systemctl = shutil.which("systemctl", path=TRUSTED_SYSTEM_PATH)
    if systemctl is not None:
        capabilities.update({"service.status", "service.action"})

    journalctl = shutil.which("journalctl", path=TRUSTED_SYSTEM_PATH)
    if journalctl is not None:
        capabilities.add("service.logs")

    if roots:
        capabilities.update(
            {
                "filesystem.discovery",
                "filesystem.search",
                "file.read",
                "file.write",
                "file.edit",
                "command.cwd",
            }
        )
        if settings.operation_mode == "personal":
            capabilities.add("filesystem.mutate")
        if shutil.which("git", path=TRUSTED_GIT_PATH) is not None:
            capabilities.add("git.status")

    configured = settings.executable_allowlist.intersection(
        generic_executables_for_mode(settings.operation_mode)
    )
    if any(
        resolve_generic_executable(name, search_path=settings.command_search_path) is not None
        for name in configured
    ):
        capabilities.update({"command.execute", "command.session"})

    if os.name == "posix" and any(
        resolve_pty_executable(name, search_path=settings.command_search_path) is not None
        for name in settings.pty_executable_allowlist
    ):
        capabilities.add("command.pty")

    return sorted(capabilities)

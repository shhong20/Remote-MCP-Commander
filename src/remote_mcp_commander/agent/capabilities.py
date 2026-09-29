from __future__ import annotations

import os
import shutil
from pathlib import Path

from remote_mcp_commander.agent.diagnostics import TRUSTED_SYSTEM_PATH
from remote_mcp_commander.agent.git_ops import TRUSTED_GIT_PATH
from remote_mcp_commander.agent.pty_ops import resolve_pty_executable
from remote_mcp_commander.config import Settings
from remote_mcp_commander.policy import (
    SAFE_GENERIC_EXECUTABLES,
    TRUSTED_GENERIC_EXEC_PATH,
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
                "file.read",
                "file.write",
            }
        )
        if shutil.which("git", path=TRUSTED_GIT_PATH) is not None:
            capabilities.add("git.status")

    configured_safe = settings.executable_allowlist.intersection(SAFE_GENERIC_EXECUTABLES)
    if any(
        resolve_generic_executable(name, search_path=TRUSTED_GENERIC_EXEC_PATH) is not None
        for name in configured_safe
    ):
        capabilities.update({"command.execute", "command.session"})

    if os.name == "posix" and any(
        resolve_pty_executable(name, search_path=TRUSTED_GENERIC_EXEC_PATH) is not None
        for name in settings.pty_executable_allowlist
    ):
        capabilities.add("command.pty")

    return sorted(capabilities)

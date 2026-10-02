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
from remote_mcp_commander.protocol import CommandDiscoveryResult

BASE_CAPABILITIES = {
    "command.discovery",
    "device.ping",
    "diagnostics.system_health",
    "diagnostics.port_lookup",
    "process.list",
    "process.info",
    "process.terminate",
    "process.signal",
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
                "filesystem.tree_list",
                "filesystem.search",
                "filesystem.search_session",
                "filesystem.tree",
                "file.read",
                "file.read_lines",
                "file.tail",
                "file.read_many",
                "file.binary_read",
                "file.binary_write",
                "file.transfer_upload",
                "file.transfer_download",
                "document.preview",
                "document.edit",
                "pdf.compose",
                "image.preview",
                "file.write",
                "file.append",
                "file.edit",
                "command.cwd",
            }
        )
        if settings.operation_mode == "personal":
            capabilities.update({"filesystem.mutate", "filesystem.tree_mutate"})
        if shutil.which("git", path=TRUSTED_GIT_PATH) is not None:
            capabilities.add("git.status")

    if settings.personal_mode:
        capabilities.add("command.env")

    configured = settings.executable_allowlist
    if not settings.personal_mode:
        configured = configured.intersection(generic_executables_for_mode(settings.operation_mode))
    if any(
        resolve_generic_executable(name, search_path=settings.command_search_path) is not None
        for name in configured
    ):
        capabilities.update(
            {
                "command.execute",
                "command.session",
                "command.stdin",
                "command.timeout",
                "command.output_lines",
            }
        )

    if os.name == "posix" and any(
        resolve_pty_executable(name, search_path=settings.command_search_path) is not None
        for name in settings.pty_executable_allowlist
    ):
        capabilities.update({"command.pty", "command.pty_output_lines"})

    if {"command.output_lines", "command.pty_output_lines"}.intersection(capabilities):
        capabilities.add("command.output_wait")

    if {"command.session", "command.pty"}.intersection(capabilities):
        capabilities.add("command.session_list")

    if (
        settings.personal_mode
        and not settings.personal_process_approval_required
        and {"command.session", "command.pty"}.intersection(capabilities)
    ):
        capabilities.add("command.session_signal")

    return sorted(capabilities)


def discover_commands(settings: Settings, request_id: str) -> CommandDiscoveryResult:
    generic_profiles = settings.executable_allowlist
    if not settings.personal_mode:
        generic_profiles = generic_profiles.intersection(
            generic_executables_for_mode(settings.operation_mode)
        )
    generic_profiles = sorted(generic_profiles)
    generic_available: list[str] = []
    generic_unavailable: list[str] = []
    for name in generic_profiles:
        target = resolve_generic_executable(name, search_path=settings.command_search_path)
        (generic_available if target is not None else generic_unavailable).append(name)

    pty_profiles = sorted(settings.pty_executable_allowlist)
    pty_available: list[str] = []
    pty_unavailable: list[str] = []
    for name in pty_profiles:
        target = (
            resolve_pty_executable(name, search_path=settings.command_search_path)
            if os.name == "posix"
            else None
        )
        (pty_available if target is not None else pty_unavailable).append(name)

    return CommandDiscoveryResult(
        request_id=request_id,
        operation_mode=settings.operation_mode,
        generic_available=generic_available,
        generic_unavailable=generic_unavailable,
        pty_available=pty_available,
        pty_unavailable=pty_unavailable,
    )

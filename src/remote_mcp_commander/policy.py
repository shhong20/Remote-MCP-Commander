from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

SAFE_GENERIC_EXECUTABLES = frozenset({"echo", "hostname", "uptime", "whoami"})
NO_ARGUMENT_EXECUTABLES = frozenset({"hostname", "uptime", "whoami"})
PERSONAL_GENERIC_EXECUTABLES = frozenset(
    {
        "awk",
        "bash",
        "cat",
        "cmake",
        "cp",
        "curl",
        "cut",
        "df",
        "docker",
        "docker-compose",
        "du",
        "echo",
        "file",
        "find",
        "free",
        "gcc",
        "g++",
        "git",
        "grep",
        "gunzip",
        "head",
        "hostname",
        "journalctl",
        "lsof",
        "ls",
        "make",
        "mkdir",
        "mv",
        "node",
        "npm",
        "npx",
        "pip",
        "pip3",
        "pnpm",
        "ps",
        "pwd",
        "pytest",
        "python",
        "python3",
        "readlink",
        "realpath",
        "rg",
        "rm",
        "rmdir",
        "sed",
        "sh",
        "sort",
        "ss",
        "stat",
        "systemctl",
        "tail",
        "tar",
        "tee",
        "touch",
        "tr",
        "tree",
        "uniq",
        "unzip",
        "uptime",
        "wc",
        "wget",
        "which",
        "whoami",
        "xargs",
        "yarn",
        "zip",
    }
)
PERSONAL_PTY_EXECUTABLES = frozenset({"bash", "sh", "python", "python3", "node"})
TRUSTED_GENERIC_EXEC_PATH = "/usr/bin:/bin"
PTY_EXECUTABLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")


def generic_executables_for_mode(mode: str) -> frozenset[str]:
    return PERSONAL_GENERIC_EXECUTABLES if mode == "personal" else SAFE_GENERIC_EXECUTABLES


def validate_generic_argv(argv: list[str], *, mode: str = "hardened") -> str | None:
    if not argv:
        return "empty argv"
    executable = Path(argv[0]).name
    if argv[0] != executable or "/" in argv[0] or "\\" in argv[0]:
        return "generic executable must be a bare name"
    if any("\x00" in argument for argument in argv):
        return "generic arguments must not contain NUL bytes"
    allowed = generic_executables_for_mode(mode)
    if executable not in allowed:
        if mode == "hardened":
            return f"executable has no safe generic profile: {executable}"
        return f"executable has no personal generic profile: {executable}"
    if mode == "hardened" and executable in NO_ARGUMENT_EXECUTABLES and len(argv) != 1:
        return f"arguments are not permitted for generic {executable}"
    return None


def validate_pty_argv(argv: list[str]) -> str | None:
    if not argv:
        return "argv must not be empty"
    executable = argv[0]
    if PTY_EXECUTABLE_RE.fullmatch(executable) is None or Path(executable).name != executable:
        return "PTY executable must be a bare executable name"
    if any("\x00" in argument for argument in argv):
        return "PTY arguments must not contain NUL bytes"
    return None


def pty_approval_target(argv: list[str], cwd: str | None = None) -> str:
    payload: object = argv if cwd is None else {"argv": argv, "cwd": cwd}
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"argv-sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def resolve_generic_executable(
    executable: str, *, search_path: str = TRUSTED_GENERIC_EXEC_PATH
) -> str | None:
    if executable != Path(executable).name or "/" in executable or "\\" in executable:
        return None
    return shutil.which(executable, path=search_path)

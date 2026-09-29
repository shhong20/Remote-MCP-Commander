from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

SAFE_GENERIC_EXECUTABLES = frozenset({"echo", "hostname", "uptime", "whoami"})
NO_ARGUMENT_EXECUTABLES = frozenset({"hostname", "uptime", "whoami"})
TRUSTED_GENERIC_EXEC_PATH = "/usr/bin:/bin"
PTY_EXECUTABLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")


def validate_generic_argv(argv: list[str]) -> str | None:
    if not argv:
        return "empty argv"
    executable = Path(argv[0]).name
    if argv[0] != executable or "/" in argv[0] or "\\" in argv[0]:
        return "generic executable must be a bare name"
    if executable not in SAFE_GENERIC_EXECUTABLES:
        return f"executable has no safe generic profile: {executable}"
    if executable in NO_ARGUMENT_EXECUTABLES and len(argv) != 1:
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


def pty_approval_target(argv: list[str]) -> str:
    canonical = json.dumps(argv, ensure_ascii=False, separators=(",", ":"))
    return f"argv-sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def resolve_generic_executable(
    executable: str,
    *,
    search_path: str = TRUSTED_GENERIC_EXEC_PATH,
) -> str | None:
    if executable != Path(executable).name or "/" in executable or "\\" in executable:
        return None
    return shutil.which(executable, path=search_path)

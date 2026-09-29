from __future__ import annotations

import shutil
from pathlib import Path

SAFE_GENERIC_EXECUTABLES = frozenset({"echo", "hostname", "uptime", "whoami"})
NO_ARGUMENT_EXECUTABLES = frozenset({"hostname", "uptime", "whoami"})
TRUSTED_GENERIC_EXEC_PATH = "/usr/bin:/bin"


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


def resolve_generic_executable(
    executable: str,
    *,
    search_path: str = TRUSTED_GENERIC_EXEC_PATH,
) -> str | None:
    if executable != Path(executable).name or "/" in executable or "\\" in executable:
        return None
    return shutil.which(executable, path=search_path)

from __future__ import annotations

from pathlib import Path

SAFE_GENERIC_EXECUTABLES = frozenset({"echo", "hostname", "uptime", "whoami"})
NO_ARGUMENT_EXECUTABLES = frozenset({"hostname", "uptime", "whoami"})


def validate_generic_argv(argv: list[str]) -> str | None:
    if not argv:
        return "empty argv"
    executable = Path(argv[0]).name
    if executable not in SAFE_GENERIC_EXECUTABLES:
        return f"executable has no safe generic profile: {executable}"
    if executable in NO_ARGUMENT_EXECUTABLES and len(argv) != 1:
        return f"arguments are not permitted for generic {executable}"
    return None

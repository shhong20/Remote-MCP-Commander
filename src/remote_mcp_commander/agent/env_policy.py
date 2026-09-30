from __future__ import annotations

import re

ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_ENV_VARS = 32
MAX_ENV_KEY_CHARS = 64
MAX_ENV_VALUE_CHARS = 4096
MAX_ENV_TOTAL_BYTES = 16_384

DENIED_ENV_KEYS = {
    "BASH_ENV",
    "ENV",
    "GCONV_PATH",
    "LD_AUDIT",
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
}


def merge_command_env(
    base: dict[str, str],
    overrides: dict[str, str],
    *,
    personal_mode: bool,
) -> dict[str, str]:
    if not overrides:
        return dict(base)
    if not personal_mode:
        raise PermissionError("environment overrides require Personal mode")
    if len(overrides) > MAX_ENV_VARS:
        raise ValueError("too many environment overrides")

    total = 0
    merged = dict(base)
    for key, value in overrides.items():
        if len(key) > MAX_ENV_KEY_CHARS or ENV_KEY_RE.fullmatch(key) is None:
            raise ValueError("invalid environment variable name")
        if len(value) > MAX_ENV_VALUE_CHARS:
            raise ValueError(f"environment value is too long: {key}")
        if key.startswith("COMMANDER_") or key in DENIED_ENV_KEYS:
            raise PermissionError(f"environment variable is not allowed: {key}")
        if "\x00" in value:
            raise ValueError(f"environment value contains NUL: {key}")
        total += len(key.encode()) + len(value.encode())
        if total > MAX_ENV_TOTAL_BYTES:
            raise ValueError("environment overrides exceed total size limit")
        merged[key] = value
    return merged

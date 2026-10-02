from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

OPERATOR_INT_FIELDS = frozenset(
    {
        "max_output_bytes",
        "session_input_max_bytes",
        "session_max_active",
        "session_history_limit",
        "pty_max_active",
        "pty_input_max_bytes",
        "file_max_bytes",
        "transfer_max_bytes",
        "transfer_session_ttl_s",
        "transfer_max_active",
    }
)
OPERATOR_FLOAT_FIELDS = frozenset(
    {
        "exec_timeout_s",
        "session_timeout_s",
        "pty_timeout_s",
        "transfer_request_timeout_s",
    }
)
OPERATOR_MUTABLE_FIELDS = frozenset(OPERATOR_INT_FIELDS | OPERATOR_FLOAT_FIELDS)
MAX_OPERATOR_CONFIG_BYTES = 65_536


def _validate_value(key: str, value: object) -> int | float:
    if key not in OPERATOR_MUTABLE_FIELDS:
        raise ValueError(f"operator config key is not mutable: {key}")
    if isinstance(value, bool):
        raise ValueError(f"operator config value for {key} must be numeric, not boolean")
    if key in OPERATOR_INT_FIELDS:
        if not isinstance(value, int):
            raise ValueError(f"operator config value for {key} must be an integer")
        return value
    if not isinstance(value, (int, float)):
        raise ValueError(f"operator config value for {key} must be numeric")
    return float(value)


def normalize_operator_overrides(
    overrides: dict[str, int | float],
) -> dict[str, int | float]:
    return {key: _validate_value(key, value) for key, value in overrides.items()}


def read_operator_overrides(path: Path) -> dict[str, int | float]:
    path = path.expanduser()
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("operator config must be a regular non-symlink file")
    if os.name != "nt":
        if info.st_uid != os.getuid():
            raise ValueError("operator config must be owned by the service user")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("operator config must not be group/world accessible")
    if info.st_size > MAX_OPERATOR_CONFIG_BYTES:
        raise ValueError("operator config exceeds size limit")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("operator config is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("operator config must be a JSON object")
    result: dict[str, int | float] = {}
    for raw_key, value in payload.items():
        if not isinstance(raw_key, str):
            raise ValueError("operator config keys must be strings")
        result[raw_key] = _validate_value(raw_key, value)
    return result


def write_operator_overrides(path: Path, overrides: dict[str, int | float]) -> None:
    path = path.expanduser()
    normalized = normalize_operator_overrides(overrides)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved_parent = parent.resolve(strict=True)
    if not resolved_parent.is_dir():
        raise ValueError("operator config parent must be a directory")
    if path.exists() and path.is_symlink():
        raise ValueError("operator config must not be a symlink")

    encoded = (json.dumps(normalized, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    if len(encoded) > MAX_OPERATOR_CONFIG_BYTES:
        raise ValueError("operator config exceeds size limit")

    fd, temp_name = tempfile.mkstemp(prefix=".operator-config-", dir=resolved_parent)
    temp_path = Path(temp_name)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        fd = -1
        os.replace(temp_path, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
            dir_flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                dir_flags |= os.O_DIRECTORY
            dir_fd = os.open(resolved_parent, dir_flags)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        temp_path.unlink(missing_ok=True)

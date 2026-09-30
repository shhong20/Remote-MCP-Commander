from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from remote_mcp_commander.agent.file_ops import file_sha256
from remote_mcp_commander.protocol import FileEditResult


def _inside(path: Path, roots: list[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _resolve_edit_target(raw_path: str, roots: list[Path]) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise PermissionError("file path must be absolute")
    parent = path.parent.resolve(strict=True)
    if not _inside(parent, roots):
        raise PermissionError("path is outside configured allowed roots")
    candidate = parent / path.name
    if candidate.is_symlink() or not candidate.is_file():
        raise PermissionError("edit target must be a regular non-symlink file")
    return candidate


def _edit_file_sync(
    request_id: str,
    raw_path: str,
    old_text: str,
    new_text: str,
    *,
    roots: list[Path],
    replace_all: bool,
    max_file_bytes: int,
) -> FileEditResult:
    try:
        path = _resolve_edit_target(raw_path, roots)
        if path.stat().st_size > max_file_bytes:
            raise ValueError("file exceeds size limit")
        original_raw = path.read_bytes()
        if b"\x00" in original_raw:
            raise ValueError("binary files are not supported")
        original = original_raw.decode("utf-8")
        original_hash = file_sha256(path)
        occurrences = original.count(old_text)
        if occurrences == 0:
            raise ValueError("old_text was not found")
        if occurrences > 1 and not replace_all:
            raise ValueError("old_text is ambiguous; multiple matches found")
        replacements = occurrences if replace_all else 1
        updated = original.replace(old_text, new_text, -1 if replace_all else 1)
        encoded = updated.encode("utf-8")
        if len(encoded) > max_file_bytes:
            raise ValueError("edited file exceeds size limit")
        mode = path.stat().st_mode & 0o777
        fd, temp_name = tempfile.mkstemp(prefix=".remote-mcp-edit-", dir=path.parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                os.chmod(temp_path, mode)
            if path.is_symlink() or file_sha256(path) != original_hash:
                raise RuntimeError("file changed during edit")
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)
        return FileEditResult(
            request_id=request_id,
            path=str(path),
            replacements=replacements,
            bytes_written=len(encoded),
            sha256=file_sha256(path),
        )
    except (OSError, PermissionError, UnicodeDecodeError, ValueError, RuntimeError) as exc:
        return FileEditResult(
            request_id=request_id,
            path=raw_path,
            rejected=True,
            error=str(exc),
        )


async def edit_text_file(
    request_id: str,
    raw_path: str,
    old_text: str,
    new_text: str,
    *,
    roots: list[Path],
    replace_all: bool,
    max_file_bytes: int,
) -> FileEditResult:
    return await asyncio.to_thread(
        _edit_file_sync,
        request_id,
        raw_path,
        old_text,
        new_text,
        roots=roots,
        replace_all=replace_all,
        max_file_bytes=max_file_bytes,
    )

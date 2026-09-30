from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import tempfile
from pathlib import Path

from remote_mcp_commander.protocol import (
    FileAppendResult,
    FileReadManyItem,
    FileReadManyResult,
    FileReadResult,
    FileWriteResult,
)


def allowed_roots(raw_roots: list[str]) -> list[Path]:
    roots: list[Path] = []
    for raw in raw_roots:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise ValueError(f"allowed root must be absolute: {raw}")
        resolved = path.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError(f"allowed root must be a directory: {raw}")
        roots.append(resolved)
    return roots


def resolve_allowed_path(raw_path: str, roots: list[Path]) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise PermissionError("file path must be absolute")
    candidate = path.resolve(strict=False)
    if not any(candidate == root or candidate.is_relative_to(root) for root in roots):
        raise PermissionError("path is outside configured allowed roots")
    return candidate


def resolve_allowed_directory(raw_path: str, roots: list[Path]) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise PermissionError("working directory must be absolute")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise PermissionError("working directory must be a directory")
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise PermissionError("working directory is outside configured allowed roots")
    return resolved


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_text_file_sync(
    request_id: str,
    raw_path: str,
    *,
    roots: list[Path],
    offset: int,
    max_bytes: int,
    max_file_bytes: int,
) -> FileReadResult:
    try:
        path = resolve_allowed_path(raw_path, roots)
        if not path.is_file():
            return FileReadResult(request_id=request_id, rejected=True, error="not a regular file")
        size = path.stat().st_size
        if size > max_file_bytes:
            return FileReadResult(
                request_id=request_id, rejected=True, error="file exceeds size limit"
            )
        if offset > size:
            return FileReadResult(
                request_id=request_id, rejected=True, error="offset exceeds file size"
            )

        with path.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read(max_bytes)
        if b"\x00" in chunk:
            return FileReadResult(
                request_id=request_id, rejected=True, error="binary files are not supported"
            )
        try:
            content = chunk.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.reason == "unexpected end of data" and exc.start > 0:
                chunk = chunk[: exc.start]
                content = chunk.decode("utf-8")
            else:
                return FileReadResult(
                    request_id=request_id,
                    rejected=True,
                    error="requested offset is not aligned to valid UTF-8 text",
                )
        next_offset = offset + len(chunk)
        return FileReadResult(
            request_id=request_id,
            path=str(path),
            content=content,
            size=size,
            offset=offset,
            next_offset=next_offset,
            eof=next_offset >= size,
            sha256=file_sha256(path),
        )
    except (OSError, PermissionError, ValueError) as exc:
        return FileReadResult(request_id=request_id, rejected=True, error=str(exc))


def _write_text_file_sync(
    request_id: str,
    raw_path: str,
    content: str,
    *,
    roots: list[Path],
    overwrite: bool,
    expected_sha256: str | None,
    max_file_bytes: int,
) -> FileWriteResult:
    encoded = content.encode("utf-8")
    if len(encoded) > max_file_bytes:
        return FileWriteResult(
            request_id=request_id, rejected=True, error="content exceeds size limit"
        )

    try:
        path = resolve_allowed_path(raw_path, roots)
        parent = path.parent.resolve(strict=True)
        if not parent.is_dir() or not any(
            parent == root or parent.is_relative_to(root) for root in roots
        ):
            raise PermissionError("parent directory is outside configured allowed roots")

        exists = path.exists()
        if exists and not path.is_file():
            return FileWriteResult(
                request_id=request_id, rejected=True, error="target is not a file"
            )
        if exists and not overwrite:
            return FileWriteResult(
                request_id=request_id, rejected=True, error="target already exists"
            )
        if exists:
            if expected_sha256 is None:
                return FileWriteResult(
                    request_id=request_id,
                    rejected=True,
                    error="expected_sha256 is required when overwriting",
                )
            if not secrets_match(file_sha256(path), expected_sha256):
                return FileWriteResult(
                    request_id=request_id, rejected=True, error="file changed since read"
                )

        existing_mode = path.stat().st_mode & 0o777 if exists else 0o600
        fd, temp_name = tempfile.mkstemp(prefix=".remote-mcp-", dir=parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                os.chmod(temp_path, existing_mode)
            if exists and expected_sha256 is not None:
                if not secrets_match(file_sha256(path), expected_sha256):
                    return FileWriteResult(
                        request_id=request_id,
                        rejected=True,
                        error="file changed during write",
                    )
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)

        return FileWriteResult(
            request_id=request_id,
            path=str(path),
            bytes_written=len(encoded),
            sha256=file_sha256(path),
        )
    except (OSError, PermissionError, ValueError) as exc:
        return FileWriteResult(request_id=request_id, rejected=True, error=str(exc))


def _append_text_file_sync(
    request_id: str,
    raw_path: str,
    content: str,
    *,
    roots: list[Path],
    expected_sha256: str | None,
    max_file_bytes: int,
) -> FileAppendResult:
    encoded = content.encode("utf-8")
    try:
        path = resolve_allowed_path(raw_path, roots).resolve(strict=True)
        if not path.is_file():
            raise PermissionError("target is not a regular file")
        flags = os.O_RDWR | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
            info = os.fstat(fd)
            if info.st_size + len(encoded) > max_file_bytes:
                raise ValueError("appended file would exceed size limit")
            os.lseek(fd, 0, os.SEEK_SET)
            current = os.read(fd, info.st_size)
            if b"\x00" in current:
                raise ValueError("binary files are not supported")
            current.decode("utf-8")
            current_hash = hashlib.sha256(current).hexdigest()
            if expected_sha256 is not None and not secrets_match(
                current_hash, expected_sha256
            ):
                raise ValueError("file changed since read")
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        final_hash = file_sha256(path)
        return FileAppendResult(
            request_id=request_id, path=str(path), bytes_appended=len(encoded),
            size=path.stat().st_size, sha256=final_hash,
        )
    except (OSError, PermissionError, UnicodeDecodeError, ValueError) as exc:
        return FileAppendResult(request_id=request_id, rejected=True, error=str(exc))


async def append_text_file(
    request_id: str, raw_path: str, content: str, *, roots: list[Path],
    expected_sha256: str | None, max_file_bytes: int,
) -> FileAppendResult:
    return await asyncio.to_thread(
        _append_text_file_sync, request_id, raw_path, content, roots=roots,
        expected_sha256=expected_sha256, max_file_bytes=max_file_bytes,
    )


def _read_many_text_files_sync(
    request_id: str,
    raw_paths: list[str],
    *,
    roots: list[Path],
    max_bytes_per_file: int,
    max_total_bytes: int,
    max_file_bytes: int,
) -> FileReadManyResult:
    files: list[FileReadManyItem] = []
    total_bytes = 0
    truncated = False
    for index, raw_path in enumerate(raw_paths):
        remaining = max_total_bytes - total_bytes
        if remaining < 4:
            truncated = True
            break
        result = _read_text_file_sync(
            request_id,
            raw_path,
            roots=roots,
            offset=0,
            max_bytes=min(max_bytes_per_file, remaining),
            max_file_bytes=max_file_bytes,
        )
        content_bytes = len(result.content.encode("utf-8"))
        total_bytes += content_bytes
        files.append(
            FileReadManyItem(
                path=result.path or raw_path,
                content=result.content,
                size=result.size,
                eof=result.eof,
                sha256=result.sha256,
                rejected=result.rejected,
                error=result.error,
            )
        )
        if total_bytes >= max_total_bytes and index + 1 < len(raw_paths):
            truncated = True
            break
    return FileReadManyResult(
        request_id=request_id,
        files=files,
        requested_count=len(raw_paths),
        total_bytes=total_bytes,
        truncated=truncated,
    )


async def read_many_text_files(
    request_id: str,
    raw_paths: list[str],
    *,
    roots: list[Path],
    max_bytes_per_file: int,
    max_total_bytes: int,
    max_file_bytes: int,
) -> FileReadManyResult:
    return await asyncio.to_thread(
        _read_many_text_files_sync,
        request_id,
        raw_paths,
        roots=roots,
        max_bytes_per_file=max_bytes_per_file,
        max_total_bytes=max_total_bytes,
        max_file_bytes=max_file_bytes,
    )


async def read_text_file(
    request_id: str,
    raw_path: str,
    *,
    roots: list[Path],
    offset: int,
    max_bytes: int,
    max_file_bytes: int,
) -> FileReadResult:
    return await asyncio.to_thread(
        _read_text_file_sync,
        request_id,
        raw_path,
        roots=roots,
        offset=offset,
        max_bytes=max_bytes,
        max_file_bytes=max_file_bytes,
    )


async def write_text_file(
    request_id: str,
    raw_path: str,
    content: str,
    *,
    roots: list[Path],
    overwrite: bool,
    expected_sha256: str | None,
    max_file_bytes: int,
) -> FileWriteResult:
    return await asyncio.to_thread(
        _write_text_file_sync,
        request_id,
        raw_path,
        content,
        roots=roots,
        overwrite=overwrite,
        expected_sha256=expected_sha256,
        max_file_bytes=max_file_bytes,
    )


def secrets_match(actual: str, expected: str) -> bool:
    return secrets.compare_digest(actual, expected)

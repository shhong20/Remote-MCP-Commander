from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
import stat
import tempfile
from pathlib import Path

from remote_mcp_commander.agent.file_ops import resolve_allowed_path, secrets_match
from remote_mcp_commander.protocol import BinaryReadResult, BinaryWriteResult


def _read_regular_file(path: Path, *, max_file_bytes: int) -> tuple[bytes, int]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PermissionError("not a regular file")
        if info.st_size > max_file_bytes:
            raise ValueError("file exceeds size limit")
        chunks: list[bytes] = []
        remaining = max_file_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > max_file_bytes:
            raise ValueError("file exceeds size limit")
        if len(data) != info.st_size:
            raise ValueError("file size changed while reading")
        return data, stat.S_IMODE(info.st_mode)
    finally:
        os.close(fd)


def _read_binary_file_sync(
    request_id: str,
    raw_path: str,
    *,
    roots: list[Path],
    offset: int,
    max_bytes: int,
    max_file_bytes: int,
) -> BinaryReadResult:
    try:
        requested = Path(raw_path).expanduser()
        if not requested.is_absolute():
            raise PermissionError("binary file path must be absolute")
        if requested.is_symlink():
            raise PermissionError("symlink binary files are not supported")
        path = resolve_allowed_path(raw_path, roots).resolve(strict=True)
        data, _ = _read_regular_file(path, max_file_bytes=max_file_bytes)
        size = len(data)
        if offset > size:
            raise ValueError("offset exceeds file size")
        chunk = data[offset : offset + max_bytes]
        next_offset = offset + len(chunk)
        return BinaryReadResult(
            request_id=request_id,
            path=str(path),
            data_base64=base64.b64encode(chunk).decode("ascii"),
            size=size,
            offset=offset,
            next_offset=next_offset,
            eof=next_offset >= size,
            sha256=hashlib.sha256(data).hexdigest(),
        )
    except (OSError, PermissionError, ValueError) as exc:
        return BinaryReadResult(request_id=request_id, rejected=True, error=str(exc))


def _write_binary_file_sync(
    request_id: str,
    raw_path: str,
    data_base64: str,
    *,
    roots: list[Path],
    overwrite: bool,
    expected_sha256: str | None,
    max_file_bytes: int,
) -> BinaryWriteResult:
    try:
        try:
            data = base64.b64decode(data_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("binary payload is invalid base64") from exc
        if len(data) > max_file_bytes:
            raise ValueError("binary payload exceeds size limit")

        requested = Path(raw_path).expanduser()
        if not requested.is_absolute():
            raise PermissionError("binary file path must be absolute")
        if requested.is_symlink():
            raise PermissionError("symlink binary files are not supported")
        path = resolve_allowed_path(raw_path, roots)
        parent = path.parent.resolve(strict=True)
        if not parent.is_dir() or not any(
            parent == root or parent.is_relative_to(root) for root in roots
        ):
            raise PermissionError("parent directory is outside configured allowed roots")

        exists = path.exists()
        existing_mode = 0o600
        if exists:
            if not overwrite:
                raise ValueError("target already exists")
            if expected_sha256 is None:
                raise ValueError("expected_sha256 is required when overwriting")
            current, existing_mode = _read_regular_file(
                path, max_file_bytes=max_file_bytes
            )
            current_hash = hashlib.sha256(current).hexdigest()
            if not secrets_match(current_hash, expected_sha256):
                raise ValueError("file changed since read")

        fd, temp_name = tempfile.mkstemp(prefix=".remote-mcp-bin-", dir=parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                os.chmod(temp_path, existing_mode)

            if exists:
                if path.is_symlink():
                    raise PermissionError("target changed to a symlink during write")
                try:
                    current, _ = _read_regular_file(
                        path, max_file_bytes=max_file_bytes
                    )
                except FileNotFoundError as exc:
                    raise ValueError("file changed during write") from exc
                if expected_sha256 is None or not secrets_match(
                    hashlib.sha256(current).hexdigest(), expected_sha256
                ):
                    raise ValueError("file changed during write")
                os.replace(temp_path, path)
            else:
                try:
                    os.link(temp_path, path)
                except FileExistsError as exc:
                    raise ValueError("target appeared during write") from exc
                temp_path.unlink()
        finally:
            temp_path.unlink(missing_ok=True)

        return BinaryWriteResult(
            request_id=request_id,
            path=str(path),
            bytes_written=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )
    except (OSError, PermissionError, ValueError) as exc:
        return BinaryWriteResult(request_id=request_id, rejected=True, error=str(exc))


async def read_binary_file(
    request_id: str,
    raw_path: str,
    *,
    roots: list[Path],
    offset: int,
    max_bytes: int,
    max_file_bytes: int,
) -> BinaryReadResult:
    return await asyncio.to_thread(
        _read_binary_file_sync,
        request_id,
        raw_path,
        roots=roots,
        offset=offset,
        max_bytes=max_bytes,
        max_file_bytes=max_file_bytes,
    )


async def write_binary_file(
    request_id: str,
    raw_path: str,
    data_base64: str,
    *,
    roots: list[Path],
    overwrite: bool,
    expected_sha256: str | None,
    max_file_bytes: int,
) -> BinaryWriteResult:
    return await asyncio.to_thread(
        _write_binary_file_sync,
        request_id,
        raw_path,
        data_base64,
        roots=roots,
        overwrite=overwrite,
        expected_sha256=expected_sha256,
        max_file_bytes=max_file_bytes,
    )

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
import stat
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from remote_mcp_commander.agent.file_ops import resolve_allowed_path, secrets_match
from remote_mcp_commander.protocol import (
    DownloadChunkResult,
    DownloadStartResult,
    TransferCloseResult,
    TransferStatusResult,
    UploadChunkResult,
    UploadFinishResult,
    UploadStartResult,
)

TRANSFER_CHUNK_BYTES = 262_144


@dataclass
class _UploadSession:
    session_id: str
    path: Path
    temp_path: Path
    fd: int
    size: int
    sha256: str
    overwrite: bool
    expected_sha256: str | None
    existed: bool
    existing_fingerprint: tuple[int, int, int, int, int] | None
    existing_mode: int
    received: int
    hasher: object
    deadline: float
    expires_at: datetime


@dataclass
class _DownloadSession:
    session_id: str
    path: Path
    fd: int
    size: int
    sha256: str
    fingerprint: tuple[int, int, int, int, int]
    chunk_hashes: tuple[str, ...]
    served: int
    deadline: float
    expires_at: datetime


def _fingerprint(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _expiry(ttl_s: int) -> tuple[float, datetime]:
    return time.monotonic() + ttl_s, datetime.now(UTC) + timedelta(seconds=ttl_s)


def _open_regular(path: Path, *, max_bytes: int) -> tuple[int, os.stat_result]:
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise PermissionError("not a regular non-symlink file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PermissionError("not a regular file")
        if (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino):
            raise PermissionError("file changed while opening")
        if info.st_size > max_bytes:
            raise ValueError("file exceeds transfer size limit")
        return fd, info
    except Exception:
        os.close(fd)
        raise


def _hash_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1_048_576)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def _hash_fd_with_chunks(fd: int) -> tuple[str, tuple[str, ...]]:
    digest = hashlib.sha256()
    chunk_hashes: list[str] = []
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, TRANSFER_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
        chunk_hashes.append(hashlib.sha256(chunk).hexdigest())
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest(), tuple(chunk_hashes)


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _prepare_upload(
    raw_path: str,
    *,
    roots: list[Path],
    size: int,
    overwrite: bool,
    expected_sha256: str | None,
    max_bytes: int,
) -> tuple[Path, Path, int, bool, tuple[int, int, int, int, int] | None, int]:
    if size > max_bytes:
        raise ValueError("upload exceeds transfer size limit")
    requested = Path(raw_path).expanduser()
    if not requested.is_absolute():
        raise PermissionError("upload path must be absolute")
    if requested.is_symlink():
        raise PermissionError("symlink upload targets are not supported")
    path = resolve_allowed_path(raw_path, roots)
    parent = path.parent.resolve(strict=True)
    if not parent.is_dir() or not any(
        parent == root or parent.is_relative_to(root) for root in roots
    ):
        raise PermissionError("parent directory is outside configured allowed roots")

    existed = path.exists()
    existing_fingerprint = None
    existing_mode = 0o600
    if existed:
        if path.is_symlink():
            raise PermissionError("symlink upload targets are not supported")
        if not overwrite:
            raise FileExistsError("target already exists")
        if expected_sha256 is None:
            raise ValueError("expected_sha256 is required when overwriting")
        current_fd, info = _open_regular(path, max_bytes=max_bytes)
        try:
            current_hash = _hash_fd(current_fd)
            after = os.fstat(current_fd)
        finally:
            os.close(current_fd)
        if _fingerprint(info) != _fingerprint(after):
            raise ValueError("target changed while hashing")
        if not secrets_match(current_hash, expected_sha256):
            raise ValueError("file changed since read")
        existing_fingerprint = _fingerprint(after)
        existing_mode = after.st_mode & 0o777
    elif path.is_symlink():
        raise PermissionError("symlink upload targets are not supported")

    fd, temp_name = tempfile.mkstemp(prefix=".remote-mcp-upload-", dir=parent)
    temp_path = Path(temp_name)
    return path, temp_path, fd, existed, existing_fingerprint, existing_mode


def _write_chunk(fd: int, offset: int, data: bytes) -> None:
    written = 0
    while written < len(data):
        if hasattr(os, "pwrite"):
            count = os.pwrite(fd, data[written:], offset + written)
        else:
            os.lseek(fd, offset + written, os.SEEK_SET)
            count = os.write(fd, data[written:])
        if count <= 0:
            raise OSError("short write while uploading")
        written += count


def _read_chunk(fd: int, offset: int, length: int) -> bytes:
    if hasattr(os, "pread"):
        return os.pread(fd, length, offset)
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, length)


def _verify_existing_target(session: _UploadSession, max_bytes: int) -> None:
    fd, info = _open_regular(session.path, max_bytes=max_bytes)
    try:
        current_hash = _hash_fd(fd)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (
        session.existing_fingerprint != _fingerprint(info)
        or _fingerprint(info) != _fingerprint(after)
    ):
        raise ValueError("target changed during upload")
    if session.expected_sha256 is None or not secrets_match(
        current_hash, session.expected_sha256
    ):
        raise ValueError("target changed during upload")


def _publish_upload(session: _UploadSession, max_bytes: int) -> None:
    if session.fd < 0:
        raise ValueError("upload temporary file is no longer open")
    os.fsync(session.fd)
    if session.existed:
        _verify_existing_target(session, max_bytes)
        if session.path.is_symlink():
            raise PermissionError("target changed to a symlink during upload")
        if os.name != "nt":
            os.fchmod(session.fd, session.existing_mode)
        else:
            os.chmod(session.temp_path, session.existing_mode)
        os.fsync(session.fd)
        os.close(session.fd)
        session.fd = -1
        os.replace(session.temp_path, session.path)
    else:
        os.close(session.fd)
        session.fd = -1
        try:
            os.link(session.temp_path, session.path)
        except FileExistsError as exc:
            raise ValueError("target appeared during upload") from exc
        session.temp_path.unlink()
    _fsync_directory(session.path.parent)


def _prepare_download(
    raw_path: str, *, roots: list[Path], max_bytes: int
) -> tuple[Path, int, os.stat_result, str, tuple[str, ...]]:
    requested = Path(raw_path).expanduser()
    if not requested.is_absolute():
        raise PermissionError("download path must be absolute")
    if requested.is_symlink():
        raise PermissionError("symlink downloads are not supported")
    path = resolve_allowed_path(raw_path, roots).resolve(strict=True)
    fd, info = _open_regular(path, max_bytes=max_bytes)
    try:
        sha256, chunk_hashes = _hash_fd_with_chunks(fd)
        after = os.fstat(fd)
        if _fingerprint(info) != _fingerprint(after):
            raise ValueError("file changed while preparing download")
        return path, fd, after, sha256, chunk_hashes
    except Exception:
        os.close(fd)
        raise


def _verified_download_read(
    session: _DownloadSession, offset: int, length: int
) -> bytes:
    info = os.fstat(session.fd)
    if _fingerprint(info) != session.fingerprint:
        raise ValueError("file changed during download")
    current = session.path.lstat()
    if stat.S_ISLNK(current.st_mode) or _fingerprint(current) != session.fingerprint:
        raise ValueError("download path changed during transfer")
    if length == 0:
        return b""

    first_index = offset // TRANSFER_CHUNK_BYTES
    last_index = (offset + length - 1) // TRANSFER_CHUNK_BYTES
    verify_start = first_index * TRANSFER_CHUNK_BYTES
    verify_end = min(session.size, (last_index + 1) * TRANSFER_CHUNK_BYTES)
    verified = _read_chunk(session.fd, verify_start, verify_end - verify_start)
    if len(verified) != verify_end - verify_start:
        raise ValueError("file changed during download")

    for index in range(first_index, last_index + 1):
        chunk_start = (index - first_index) * TRANSFER_CHUNK_BYTES
        expected_len = min(
            TRANSFER_CHUNK_BYTES,
            session.size - index * TRANSFER_CHUNK_BYTES,
        )
        chunk = verified[chunk_start : chunk_start + expected_len]
        if hashlib.sha256(chunk).hexdigest() != session.chunk_hashes[index]:
            raise ValueError("file content changed during download")

    relative = offset - verify_start
    return verified[relative : relative + length]


class FileTransferManager:
    def __init__(
        self,
        *,
        roots: list[Path],
        max_bytes: int,
        ttl_s: int,
        max_active: int,
    ) -> None:
        self.roots = roots
        self.max_bytes = max_bytes
        self.ttl_s = ttl_s
        self.max_active = max_active
        self._sessions: dict[str, _UploadSession | _DownloadSession] = {}
        self._lock = asyncio.Lock()

    def _touch(self, session: _UploadSession | _DownloadSession) -> None:
        session.deadline, session.expires_at = _expiry(self.ttl_s)

    def _close_session(self, session: _UploadSession | _DownloadSession) -> None:
        if session.fd >= 0:
            try:
                os.close(session.fd)
            except OSError:
                pass
            session.fd = -1
        if isinstance(session, _UploadSession):
            session.temp_path.unlink(missing_ok=True)

    def _reap_locked(self) -> None:
        now = time.monotonic()
        expired = [sid for sid, item in self._sessions.items() if item.deadline <= now]
        for session_id in expired:
            session = self._sessions.pop(session_id)
            self._close_session(session)

    async def reaper_loop(self) -> None:
        delay = min(30.0, max(5.0, self.ttl_s / 4))
        try:
            while True:
                await asyncio.sleep(delay)
                async with self._lock:
                    self._reap_locked()
        except asyncio.CancelledError:
            raise

    async def close_all(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            for session in sessions:
                self._close_session(session)

    async def start_upload(
        self,
        request_id: str,
        session_id: str,
        path: str,
        *,
        size: int,
        sha256: str,
        overwrite: bool,
        expected_sha256: str | None,
    ) -> UploadStartResult:
        async with self._lock:
            self._reap_locked()
            if session_id in self._sessions:
                return UploadStartResult(
                    request_id=request_id, session_id=session_id, rejected=True,
                    error="transfer session already exists",
                )
            if len(self._sessions) >= self.max_active:
                return UploadStartResult(
                    request_id=request_id, session_id=session_id, rejected=True,
                    error="too many active transfer sessions",
                )
            try:
                prepared = await asyncio.to_thread(
                    _prepare_upload,
                    path,
                    roots=self.roots,
                    size=size,
                    overwrite=overwrite,
                    expected_sha256=expected_sha256,
                    max_bytes=self.max_bytes,
                )
                target, temp_path, fd, existed, fingerprint, mode = prepared
                deadline, expires_at = _expiry(self.ttl_s)
                session = _UploadSession(
                    session_id=session_id,
                    path=target,
                    temp_path=temp_path,
                    fd=fd,
                    size=size,
                    sha256=sha256,
                    overwrite=overwrite,
                    expected_sha256=expected_sha256,
                    existed=existed,
                    existing_fingerprint=fingerprint,
                    existing_mode=mode,
                    received=0,
                    hasher=hashlib.sha256(),
                    deadline=deadline,
                    expires_at=expires_at,
                )
                self._sessions[session_id] = session
                return UploadStartResult(
                    request_id=request_id,
                    session_id=session_id,
                    path=str(target),
                    size=size,
                    received=0,
                    sha256=sha256,
                    chunk_size=TRANSFER_CHUNK_BYTES,
                    expires_at=expires_at,
                )
            except (OSError, PermissionError, ValueError) as exc:
                return UploadStartResult(
                    request_id=request_id, session_id=session_id, rejected=True, error=str(exc)
                )

    async def upload_chunk(
        self,
        request_id: str,
        session_id: str,
        *,
        offset: int,
        data_base64: str,
    ) -> UploadChunkResult:
        async with self._lock:
            self._reap_locked()
            session = self._sessions.get(session_id)
            if not isinstance(session, _UploadSession):
                return UploadChunkResult(
                    request_id=request_id, session_id=session_id, rejected=True,
                    error="upload session not found",
                )
            try:
                try:
                    data = base64.b64decode(data_base64, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise ValueError("upload chunk is invalid base64") from exc
                if len(data) > TRANSFER_CHUNK_BYTES:
                    raise ValueError("upload chunk exceeds 256 KiB limit")
                if offset > session.received:
                    raise ValueError(f"upload offset mismatch; expected {session.received}")
                if offset + len(data) > session.size:
                    raise ValueError("upload chunk exceeds declared file size")
                if offset < session.received:
                    if offset + len(data) > session.received:
                        raise ValueError(f"upload offset mismatch; expected {session.received}")
                    existing = await asyncio.to_thread(
                        _read_chunk, session.fd, offset, len(data)
                    )
                    if existing != data:
                        raise ValueError("replayed upload chunk does not match accepted data")
                else:
                    await asyncio.to_thread(_write_chunk, session.fd, offset, data)
                    session.hasher.update(data)
                    session.received += len(data)
                self._touch(session)
                return UploadChunkResult(
                    request_id=request_id,
                    session_id=session_id,
                    offset=offset,
                    bytes_accepted=len(data),
                    received=session.received,
                    complete=session.received == session.size,
                    expires_at=session.expires_at,
                )
            except (OSError, PermissionError, ValueError) as exc:
                return UploadChunkResult(
                    request_id=request_id, session_id=session_id, offset=offset,
                    received=session.received, rejected=True, error=str(exc),
                )

    async def finish_upload(self, request_id: str, session_id: str) -> UploadFinishResult:
        async with self._lock:
            self._reap_locked()
            session = self._sessions.get(session_id)
            if not isinstance(session, _UploadSession):
                return UploadFinishResult(
                    request_id=request_id, session_id=session_id, rejected=True,
                    error="upload session not found",
                )
            try:
                if session.received != session.size:
                    raise ValueError(
                        f"upload incomplete: received {session.received} of {session.size} bytes"
                    )
                actual_sha256 = session.hasher.hexdigest()
                if not secrets_match(actual_sha256, session.sha256):
                    raise ValueError("uploaded content SHA-256 does not match declared SHA-256")
                await asyncio.to_thread(_publish_upload, session, self.max_bytes)
                self._sessions.pop(session_id, None)
                return UploadFinishResult(
                    request_id=request_id,
                    session_id=session_id,
                    path=str(session.path),
                    size=session.size,
                    sha256=actual_sha256,
                    committed=True,
                )
            except (OSError, PermissionError, ValueError) as exc:
                self._touch(session)
                return UploadFinishResult(
                    request_id=request_id,
                    session_id=session_id,
                    path=str(session.path),
                    size=session.size,
                    rejected=True,
                    error=str(exc),
                )

    async def start_download(
        self, request_id: str, session_id: str, path: str
    ) -> DownloadStartResult:
        async with self._lock:
            self._reap_locked()
            if session_id in self._sessions:
                return DownloadStartResult(
                    request_id=request_id, session_id=session_id, rejected=True,
                    error="transfer session already exists",
                )
            if len(self._sessions) >= self.max_active:
                return DownloadStartResult(
                    request_id=request_id, session_id=session_id, rejected=True,
                    error="too many active transfer sessions",
                )
            try:
                target, fd, info, sha256, chunk_hashes = await asyncio.to_thread(
                    _prepare_download, path, roots=self.roots, max_bytes=self.max_bytes
                )
                deadline, expires_at = _expiry(self.ttl_s)
                session = _DownloadSession(
                    session_id=session_id,
                    path=target,
                    fd=fd,
                    size=info.st_size,
                    sha256=sha256,
                    fingerprint=_fingerprint(info),
                    chunk_hashes=chunk_hashes,
                    served=0,
                    deadline=deadline,
                    expires_at=expires_at,
                )
                self._sessions[session_id] = session
                return DownloadStartResult(
                    request_id=request_id,
                    session_id=session_id,
                    path=str(target),
                    size=session.size,
                    sha256=sha256,
                    chunk_size=TRANSFER_CHUNK_BYTES,
                    expires_at=expires_at,
                )
            except (OSError, PermissionError, ValueError) as exc:
                return DownloadStartResult(
                    request_id=request_id, session_id=session_id, rejected=True, error=str(exc)
                )

    async def download_chunk(
        self,
        request_id: str,
        session_id: str,
        *,
        offset: int,
        max_bytes: int,
    ) -> DownloadChunkResult:
        async with self._lock:
            self._reap_locked()
            session = self._sessions.get(session_id)
            if not isinstance(session, _DownloadSession):
                return DownloadChunkResult(
                    request_id=request_id, session_id=session_id, rejected=True,
                    error="download session not found",
                )
            try:
                if offset > session.size:
                    raise ValueError("offset exceeds file size")
                data = await asyncio.to_thread(
                    _verified_download_read,
                    session,
                    offset,
                    min(max_bytes, TRANSFER_CHUNK_BYTES, session.size - offset),
                )
                next_offset = offset + len(data)
                session.served = max(session.served, next_offset)
                self._touch(session)
                return DownloadChunkResult(
                    request_id=request_id,
                    session_id=session_id,
                    data_base64=base64.b64encode(data).decode("ascii"),
                    size=session.size,
                    offset=offset,
                    next_offset=next_offset,
                    eof=next_offset >= session.size,
                    sha256=session.sha256,
                    expires_at=session.expires_at,
                )
            except (OSError, PermissionError, ValueError) as exc:
                return DownloadChunkResult(
                    request_id=request_id, session_id=session_id, size=session.size,
                    offset=offset, rejected=True, error=str(exc),
                )

    async def status(self, request_id: str, session_id: str) -> TransferStatusResult:
        async with self._lock:
            self._reap_locked()
            session = self._sessions.get(session_id)
            if session is None:
                return TransferStatusResult(
                    request_id=request_id, session_id=session_id, rejected=True,
                    error="transfer session not found",
                )
            self._touch(session)
            if isinstance(session, _UploadSession):
                kind = "upload"
                transferred = session.received
            else:
                kind = "download"
                transferred = session.served
            return TransferStatusResult(
                request_id=request_id,
                session_id=session_id,
                kind=kind,
                path=str(session.path),
                size=session.size,
                transferred=transferred,
                sha256=session.sha256,
                expires_at=session.expires_at,
            )

    async def close(self, request_id: str, session_id: str) -> TransferCloseResult:
        async with self._lock:
            self._reap_locked()
            session = self._sessions.pop(session_id, None)
            if session is None:
                return TransferCloseResult(
                    request_id=request_id, session_id=session_id, rejected=True,
                    error="transfer session not found",
                )
            kind = "upload" if isinstance(session, _UploadSession) else "download"
            self._close_session(session)
            return TransferCloseResult(
                request_id=request_id, session_id=session_id, kind=kind, closed=True
            )

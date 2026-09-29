from __future__ import annotations

import asyncio
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from remote_mcp_commander.agent.file_ops import resolve_allowed_path
from remote_mcp_commander.protocol import (
    DirectoryEntry,
    DirectoryListResult,
    FileInfoResult,
    FileRootListResult,
)


def _inside_roots(path: Path, roots: list[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _modified(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=UTC)


def _list_roots_sync(request_id: str, roots: list[Path]) -> FileRootListResult:
    return FileRootListResult(request_id=request_id, roots=[str(root) for root in roots])


async def list_file_roots(request_id: str, roots: list[Path]) -> FileRootListResult:
    return _list_roots_sync(request_id, roots)


def _list_directory_sync(
    request_id: str,
    raw_path: str,
    *,
    roots: list[Path],
    limit: int,
) -> DirectoryListResult:
    try:
        path = resolve_allowed_path(raw_path, roots).resolve(strict=True)
        if not path.is_dir():
            return DirectoryListResult(
                request_id=request_id,
                path=str(path),
                rejected=True,
                error="path is not a directory",
            )
        entries: list[DirectoryEntry] = []
        truncated = False
        with os.scandir(path) as iterator:
            for index, entry in enumerate(iterator):
                if index >= limit:
                    truncated = True
                    break
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except (FileNotFoundError, PermissionError):
                    continue
                kind = _kind(metadata.st_mode)
                entries.append(
                    DirectoryEntry(
                        name=entry.name,
                        path=str(path / entry.name),
                        kind=kind,
                        size=metadata.st_size if kind == "file" else None,
                        modified_at=_modified(metadata.st_mtime),
                    )
                )
        entries.sort(key=lambda item: (item.kind != "directory", item.name.casefold(), item.name))
        return DirectoryListResult(
            request_id=request_id,
            path=str(path),
            entries=entries,
            truncated=truncated,
        )
    except (OSError, PermissionError, ValueError) as exc:
        return DirectoryListResult(request_id=request_id, rejected=True, error=str(exc))


async def list_directory(
    request_id: str,
    raw_path: str,
    *,
    roots: list[Path],
    limit: int,
) -> DirectoryListResult:
    return await asyncio.to_thread(
        _list_directory_sync,
        request_id,
        raw_path,
        roots=roots,
        limit=limit,
    )


def _lstat_allowed_path(raw_path: str, roots: list[Path]) -> tuple[Path, os.stat_result]:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise PermissionError("file path must be absolute")
    lexical = Path(os.path.abspath(path))
    if lexical in roots:
        return lexical, lexical.lstat()

    parent = path.parent.resolve(strict=True)
    if not _inside_roots(parent, roots):
        raise PermissionError("path is outside configured allowed roots")
    candidate = parent / path.name
    return candidate, candidate.lstat()


def _file_info_sync(request_id: str, raw_path: str, *, roots: list[Path]) -> FileInfoResult:
    try:
        path, metadata = _lstat_allowed_path(raw_path, roots)
        kind = _kind(metadata.st_mode)
        return FileInfoResult(
            request_id=request_id,
            path=str(path),
            kind=kind,
            size=metadata.st_size if kind == "file" else None,
            modified_at=_modified(metadata.st_mtime),
            mode=oct(stat.S_IMODE(metadata.st_mode)),
        )
    except (OSError, PermissionError, ValueError) as exc:
        return FileInfoResult(request_id=request_id, rejected=True, error=str(exc))


async def file_info(request_id: str, raw_path: str, *, roots: list[Path]) -> FileInfoResult:
    return await asyncio.to_thread(
        _file_info_sync,
        request_id,
        raw_path,
        roots=roots,
    )

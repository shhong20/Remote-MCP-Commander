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
    DirectoryTreeEntry,
    DirectoryTreeResult,
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


def _list_directory_tree_sync(
    request_id: str,
    raw_path: str,
    *,
    roots: list[Path],
    depth: int,
    include_hidden: bool,
    per_directory_limit: int,
    max_entries: int,
) -> DirectoryTreeResult:
    try:
        root = resolve_allowed_path(raw_path, roots).resolve(strict=True)
        if not root.is_dir() or not _inside_roots(root, roots):
            raise PermissionError("tree root must be a directory inside allowed roots")
        entries: list[DirectoryTreeEntry] = []
        scanned_directories = 0
        truncated = False

        def visit(directory: Path, current_depth: int) -> None:
            nonlocal scanned_directories, truncated
            if len(entries) >= max_entries:
                truncated = True
                return
            resolved = directory.resolve(strict=True)
            if not resolved.is_dir() or not _inside_roots(resolved, roots):
                truncated = True
                return
            scanned_directories += 1
            try:
                with os.scandir(resolved) as iterator:
                    raw_entries = list(iterator)
            except (OSError, PermissionError):
                truncated = True
                return
            candidates = [
                entry
                for entry in raw_entries
                if include_hidden or not entry.name.startswith(".")
            ]
            items: list[tuple[os.DirEntry[str], os.stat_result, str]] = []
            for entry in candidates:
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except (FileNotFoundError, PermissionError):
                    truncated = True
                    continue
                items.append((entry, metadata, _kind(metadata.st_mode)))
            items.sort(
                key=lambda item: (
                    item[2] != "directory",
                    item[0].name.casefold(),
                    item[0].name,
                )
            )
            if len(items) > per_directory_limit:
                items = items[:per_directory_limit]
                truncated = True
            child_directories: list[Path] = []
            for entry, metadata, kind in items:
                if len(entries) >= max_entries:
                    truncated = True
                    break
                absolute = resolved / entry.name
                entries.append(
                    DirectoryTreeEntry(
                        name=entry.name,
                        path=str(absolute),
                        relative_path=absolute.relative_to(root).as_posix(),
                        kind=kind,
                        depth=current_depth,
                        size=metadata.st_size if kind == "file" else None,
                        modified_at=_modified(metadata.st_mtime),
                    )
                )
                if kind == "directory" and current_depth < depth:
                    child_directories.append(absolute)
            for child in child_directories:
                if len(entries) >= max_entries:
                    truncated = True
                    break
                try:
                    visit(child, current_depth + 1)
                except (OSError, PermissionError):
                    truncated = True

        visit(root, 1)
        return DirectoryTreeResult(
            request_id=request_id,
            path=str(root),
            entries=entries,
            scanned_directories=scanned_directories,
            truncated=truncated,
        )
    except (OSError, PermissionError, ValueError) as exc:
        return DirectoryTreeResult(
            request_id=request_id,
            rejected=True,
            error=str(exc),
        )


async def list_directory_tree(
    request_id: str,
    raw_path: str,
    *,
    roots: list[Path],
    depth: int,
    include_hidden: bool,
    per_directory_limit: int,
    max_entries: int,
) -> DirectoryTreeResult:
    return await asyncio.to_thread(
        _list_directory_tree_sync,
        request_id,
        raw_path,
        roots=roots,        depth=depth,
        include_hidden=include_hidden,
        per_directory_limit=per_directory_limit,
        max_entries=max_entries,
    )

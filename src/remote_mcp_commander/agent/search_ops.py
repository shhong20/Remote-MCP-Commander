from __future__ import annotations

import asyncio
import fnmatch
import os
from pathlib import Path

from remote_mcp_commander.agent.file_ops import resolve_allowed_path
from remote_mcp_commander.protocol import FileSearchMatch, FileSearchResult

MAX_SCAN_FILES = 10_000
MAX_SEARCH_FILE_BYTES = 1_048_576
MAX_PREVIEW_CHARS = 300


def _inside(path: Path, roots: list[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _search_sync(
    request_id: str,
    raw_root: str,
    query: str,
    *,
    roots: list[Path],
    mode: str,
    file_glob: str | None,
    case_sensitive: bool,
    max_results: int,
) -> FileSearchResult:
    try:
        root = resolve_allowed_path(raw_root, roots).resolve(strict=True)
        if not root.is_dir() or not _inside(root, roots):
            raise PermissionError("search root must be a directory inside allowed roots")
        needle = query if case_sensitive else query.casefold()
        matches: list[FileSearchMatch] = []
        scanned = 0
        truncated = False
        for current, dirs, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            dirs[:] = [name for name in dirs if not (current_path / name).is_symlink()]
            for name in files:
                if scanned >= MAX_SCAN_FILES:
                    truncated = True
                    break
                path = current_path / name
                if path.is_symlink() or not path.is_file():
                    continue
                scanned += 1
                if file_glob and not fnmatch.fnmatch(name, file_glob):
                    continue
                if mode == "files":
                    haystack = name if case_sensitive else name.casefold()
                    if needle in haystack:
                        matches.append(FileSearchMatch(path=str(path)))
                else:
                    try:
                        if path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                            continue
                        raw = path.read_bytes()
                    except OSError:
                        continue
                    if b"\x00" in raw:
                        continue
                    text = raw.decode("utf-8", errors="replace")
                    for line_number, line in enumerate(text.splitlines(), start=1):
                        haystack = line if case_sensitive else line.casefold()
                        if needle in haystack:
                            matches.append(
                                FileSearchMatch(
                                    path=str(path),
                                    line=line_number,
                                    preview=line[:MAX_PREVIEW_CHARS],
                                )
                            )
                            if len(matches) >= max_results:
                                truncated = True
                                break
                if len(matches) >= max_results:
                    truncated = True
                    break
            if truncated:
                break
        return FileSearchResult(
            request_id=request_id, matches=matches, scanned_files=scanned, truncated=truncated
        )
    except (OSError, PermissionError, ValueError) as exc:
        return FileSearchResult(request_id=request_id, rejected=True, error=str(exc))


async def search_files(
    request_id: str,
    raw_root: str,
    query: str,
    *,
    roots: list[Path],
    mode: str,
    file_glob: str | None,
    case_sensitive: bool,
    max_results: int,
) -> FileSearchResult:
    return await asyncio.to_thread(
        _search_sync,
        request_id,
        raw_root,
        query,
        roots=roots,
        mode=mode,
        file_glob=file_glob,
        case_sensitive=case_sensitive,
        max_results=max_results,
    )

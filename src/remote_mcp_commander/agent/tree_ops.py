from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from remote_mcp_commander.protocol import TreeInspectResult, TreeMutationResult


@dataclass(frozen=True)
class _TreeSnapshot:
    path: Path
    entries: int
    total_bytes: int
    tree_sha256: str
    root_dev: int
    root_ino: int


def _inside(path: Path, roots: list[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _resolve_tree(raw_path: str, roots: list[Path], *, reject_root: bool) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise PermissionError("tree path must be absolute")
    parent = path.parent.resolve(strict=True)
    if not _inside(parent, roots):
        raise PermissionError("tree path is outside configured allowed roots")
    candidate = parent / path.name
    if candidate.is_symlink() or not candidate.is_dir():
        raise PermissionError("tree path must be a real non-symlink directory")
    resolved = candidate.resolve(strict=True)
    if not _inside(resolved, roots):
        raise PermissionError("tree path escapes configured allowed roots")
    if reject_root and resolved in roots:
        raise PermissionError("operation on an allowed root itself is not permitted")
    return resolved


def _resolve_destination(raw_path: str, roots: list[Path]) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise PermissionError("destination must be absolute")
    parent = path.parent.resolve(strict=True)
    if not _inside(parent, roots):
        raise PermissionError("destination is outside configured allowed roots")
    target = parent / path.name
    if target in roots:
        raise PermissionError("operation on an allowed root itself is not permitted")
    if target.exists() or target.is_symlink():
        raise FileExistsError("destination already exists")
    return target


def _hash_regular_file(path: Path) -> tuple[str, int]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PermissionError("tree contains a non-regular file")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(fd, 128 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        return digest.hexdigest(), size
    finally:
        os.close(fd)


def _scan_tree(path: Path, *, max_entries: int, max_total_bytes: int) -> _TreeSnapshot:
    root_info = os.lstat(path)
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise PermissionError("tree path changed during inspection")

    digest = hashlib.sha256()
    digest.update(f"R\0{stat.S_IMODE(root_info.st_mode):o}\n".encode())
    entries = 0
    total_bytes = 0
    for current, dirs, files in os.walk(path, topdown=True, followlinks=False):
        current_path = Path(current)
        dirs.sort()
        files.sort()
        for name in dirs:
            child = current_path / name
            info = os.lstat(child)
            if stat.S_ISLNK(info.st_mode):
                raise PermissionError("tree contains a symlink")
            if not stat.S_ISDIR(info.st_mode):
                raise PermissionError("tree contains a non-directory entry")
            entries += 1
            if entries > max_entries:
                raise ValueError("tree exceeds entry limit")
            relative = child.relative_to(path).as_posix()
            digest.update(f"D\0{relative}\0{stat.S_IMODE(info.st_mode):o}\n".encode())

        for name in files:
            child = current_path / name
            info = os.lstat(child)
            if stat.S_ISLNK(info.st_mode):
                raise PermissionError("tree contains a symlink")
            if not stat.S_ISREG(info.st_mode):
                raise PermissionError("tree contains a special file")
            entries += 1
            if entries > max_entries:
                raise ValueError("tree exceeds entry limit")
            file_digest, size = _hash_regular_file(child)
            total_bytes += size
            if total_bytes > max_total_bytes:
                raise ValueError("tree exceeds byte limit")
            relative = child.relative_to(path).as_posix()
            digest.update(
                f"F\0{relative}\0{stat.S_IMODE(info.st_mode):o}\0{size}\0{file_digest}\n".encode()
            )

    final_info = os.lstat(path)
    if (final_info.st_dev, final_info.st_ino) != (root_info.st_dev, root_info.st_ino):
        raise RuntimeError("tree root changed during inspection")
    return _TreeSnapshot(
        path=path,
        entries=entries,
        total_bytes=total_bytes,
        tree_sha256=digest.hexdigest(),
        root_dev=root_info.st_dev,
        root_ino=root_info.st_ino,
    )


def _inspect_tree_sync(
    request_id: str,
    raw_path: str,
    *,
    roots: list[Path],
    max_entries: int,
    max_total_bytes: int,
) -> TreeInspectResult:
    try:
        path = _resolve_tree(raw_path, roots, reject_root=False)
        snapshot = _scan_tree(path, max_entries=max_entries, max_total_bytes=max_total_bytes)
        return TreeInspectResult(
            request_id=request_id,
            path=str(path),
            entries=snapshot.entries,
            total_bytes=snapshot.total_bytes,
            tree_sha256=snapshot.tree_sha256,
        )
    except (OSError, PermissionError, ValueError, RuntimeError) as exc:
        return TreeInspectResult(
            request_id=request_id, path=raw_path, rejected=True, error=str(exc)
        )


def _cleanup_created_tree(path: Path) -> None:
    try:
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)
    except OSError:
        pass


def _mutate_tree_sync(
    request_id: str,
    operation: str,
    raw_path: str,
    *,
    roots: list[Path],
    destination: str | None,
    expected_tree_sha256: str,
    max_entries: int,
    max_total_bytes: int,
) -> TreeMutationResult:
    created_target: Path | None = None
    try:
        path = _resolve_tree(raw_path, roots, reject_root=True)
        snapshot = _scan_tree(path, max_entries=max_entries, max_total_bytes=max_total_bytes)
        if not secrets.compare_digest(snapshot.tree_sha256, expected_tree_sha256):
            raise RuntimeError("tree changed since inspection")

        if operation == "copy_tree":
            if destination is None:
                raise ValueError("destination is required")
            target = _resolve_destination(destination, roots)
            if target.is_relative_to(path):
                raise PermissionError("destination must not be inside source tree")
            shutil.copytree(path, target, symlinks=True, copy_function=shutil.copy2)
            created_target = target
            copied = _scan_tree(
                target,
                max_entries=max_entries,
                max_total_bytes=max_total_bytes,
            )
            if not secrets.compare_digest(copied.tree_sha256, snapshot.tree_sha256):
                raise RuntimeError("copied tree does not match inspected source")
            return TreeMutationResult(
                request_id=request_id,
                operation=operation,
                path=str(path),
                destination=str(target),
                entries=snapshot.entries,
                total_bytes=snapshot.total_bytes,
                changed=True,
            )

        if operation == "delete_tree":
            if destination is not None:
                raise ValueError("destination is not valid for delete_tree")
            current = os.lstat(path)
            if (current.st_dev, current.st_ino) != (snapshot.root_dev, snapshot.root_ino):
                raise RuntimeError("tree root changed before deletion")
            if os.name == "posix" and not shutil.rmtree.avoids_symlink_attacks:
                raise RuntimeError("safe recursive deletion is unavailable on this platform")
            shutil.rmtree(path)
            return TreeMutationResult(
                request_id=request_id,
                operation=operation,
                path=str(path),
                entries=snapshot.entries,
                total_bytes=snapshot.total_bytes,
                changed=True,
            )

        raise ValueError("unsupported tree operation")
    except (OSError, PermissionError, ValueError, RuntimeError) as exc:
        if created_target is not None:
            _cleanup_created_tree(created_target)
        return TreeMutationResult(
            request_id=request_id,
            operation=operation,
            path=raw_path,
            destination=destination,
            rejected=True,
            error=str(exc),
        )


async def inspect_tree(
    request_id: str,
    raw_path: str,
    *,
    roots: list[Path],
    max_entries: int,
    max_total_bytes: int,
) -> TreeInspectResult:
    return await asyncio.to_thread(
        _inspect_tree_sync,
        request_id,
        raw_path,
        roots=roots,
        max_entries=max_entries,
        max_total_bytes=max_total_bytes,
    )


async def mutate_tree(
    request_id: str,
    operation: str,
    raw_path: str,
    *,
    roots: list[Path],
    destination: str | None,
    expected_tree_sha256: str,
    max_entries: int,
    max_total_bytes: int,
) -> TreeMutationResult:
    return await asyncio.to_thread(
        _mutate_tree_sync,
        request_id,
        operation,
        raw_path,
        roots=roots,
        destination=destination,
        expected_tree_sha256=expected_tree_sha256,
        max_entries=max_entries,
        max_total_bytes=max_total_bytes,
    )

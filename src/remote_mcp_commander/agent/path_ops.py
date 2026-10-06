from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from remote_mcp_commander.protocol import PathMutationResult


def _inside(path: Path, roots: list[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _resolve_parent_target(raw_path: str, roots: list[Path]) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise PermissionError("path must be absolute")
    parent = path.parent.resolve(strict=True)
    if not _inside(parent, roots):
        raise PermissionError("path is outside configured allowed roots")
    return parent / path.name


def _reject_root(path: Path, roots: list[Path]) -> None:
    if path in roots:
        raise PermissionError("operation on an allowed root itself is not permitted")


def _resolve_existing_path(raw_path: str, roots: list[Path]) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise PermissionError("path must be absolute")
    parent = path.parent.resolve(strict=True)
    if not _inside(parent, roots):
        raise PermissionError("path is outside configured allowed roots")
    candidate = parent / path.name
    if candidate.is_symlink():
        raise PermissionError("symlink mutations are not supported")
    return candidate


def _mutate_sync(
    request_id: str,
    operation: str,
    raw_path: str,
    *,
    roots: list[Path],
    destination: str | None,
    parents: bool,
    overwrite: bool,
) -> PathMutationResult:
    try:
        if operation == "mkdir":
            raw = Path(raw_path).expanduser()
            if not raw.is_absolute():
                raise PermissionError("path must be absolute")
            if parents:
                lexical = Path(os.path.abspath(raw))
                matching_roots = [
                    root for root in roots if lexical == root or lexical.is_relative_to(root)
                ]
                if not matching_roots:
                    raise PermissionError("path is outside configured allowed roots")
                path = lexical
            else:
                path = _resolve_parent_target(raw_path, roots)
            _reject_root(path, roots)
            if path.exists() or path.is_symlink():
                if path.is_dir() and not path.is_symlink():
                    return PathMutationResult(
                        request_id=request_id,
                        operation=operation,
                        path=str(path),
                        changed=False,
                    )
                raise FileExistsError("target already exists")
            if parents:
                parent = path.parent
                missing: list[Path] = []
                while not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                parent = parent.resolve(strict=True)
                if not _inside(parent, roots):
                    raise PermissionError("parent chain escapes configured allowed roots")
                for item in reversed(missing):
                    item.mkdir()
            path.mkdir()
            return PathMutationResult(
                request_id=request_id,
                operation=operation,
                path=str(path),
                changed=True,
            )

        path = _resolve_existing_path(raw_path, roots)
        _reject_root(path, roots)
        if not path.exists():
            raise FileNotFoundError("source path does not exist")

        if operation == "delete":
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
            else:
                raise PermissionError("only regular files and empty directories can be deleted")
            return PathMutationResult(
                request_id=request_id,
                operation=operation,
                path=str(path),
                changed=True,
            )

        if destination is None:
            raise ValueError("destination is required")
        target = _resolve_parent_target(destination, roots)
        _reject_root(target, roots)
        if target.exists() or target.is_symlink():
            if not overwrite:
                raise FileExistsError("destination already exists")
            if target.is_symlink() or target.is_dir() or path.is_dir():
                raise PermissionError("overwrite supports regular files only")

        if operation == "copy":
            if not path.is_file():
                raise PermissionError("copy currently supports regular files only")
            shutil.copy2(path, target)
        elif operation == "move":
            os.replace(path, target) if overwrite else path.rename(target)
        else:
            raise ValueError("unsupported path operation")
        return PathMutationResult(
            request_id=request_id,
            operation=operation,
            path=str(path),
            destination=str(target),
            changed=True,
        )
    except (OSError, PermissionError, ValueError) as exc:
        return PathMutationResult(
            request_id=request_id,
            operation=operation,
            path=raw_path,
            destination=destination,
            rejected=True,
            error=str(exc),
        )


async def mutate_path(
    request_id: str,
    operation: str,
    raw_path: str,
    *,
    roots: list[Path],
    destination: str | None = None,
    parents: bool = False,
    overwrite: bool = False,
) -> PathMutationResult:
    return await asyncio.to_thread(
        _mutate_sync,
        request_id,
        operation,
        raw_path,
        roots=roots,
        destination=destination,
        parents=parents,
        overwrite=overwrite,
    )

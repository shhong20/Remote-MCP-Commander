from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import stat
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from remote_mcp_commander.agent.file_ops import file_sha256
from remote_mcp_commander.agent.tree_ops import _hash_regular_file, _resolve_tree, _scan_tree
from remote_mcp_commander.protocol import (
    ArchiveCreateResult,
    ArchiveEntry,
    ArchiveExtractResult,
    ArchiveInspectResult,
)

ARCHIVE_MAX_INPUT_BYTES = 268_435_456
ARCHIVE_MAX_ENTRIES = 5_000
ARCHIVE_MAX_TOTAL_BYTES = 268_435_456
ARCHIVE_LIST_LIMIT = 200
ARCHIVE_CHUNK_BYTES = 128 * 1024

_ARCHIVE_SUFFIXES = {
    ".zip": "zip",
    ".tar": "tar",
    ".tar.gz": "tar.gz",
    ".tgz": "tar.gz",
}


@dataclass(frozen=True)
class _ArchiveSnapshot:
    path: Path
    sha256: str
    dev: int
    ino: int
    size: int
    format: str


def _inside(path: Path, roots: list[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _archive_format(path: Path) -> str:
    lower = path.name.lower()
    for suffix in sorted(_ARCHIVE_SUFFIXES, key=len, reverse=True):
        if lower.endswith(suffix):
            return _ARCHIVE_SUFFIXES[suffix]
    raise ValueError("archive must use .zip, .tar, .tar.gz, or .tgz")


def _resolve_existing_archive(raw_path: str, roots: list[Path]) -> _ArchiveSnapshot:
    raw = Path(raw_path).expanduser()
    if not raw.is_absolute():
        raise PermissionError("archive path must be absolute")
    parent = raw.parent.resolve(strict=True)
    if not _inside(parent, roots):
        raise PermissionError("archive path is outside configured allowed roots")
    path = parent / raw.name
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PermissionError("archive must be a regular non-symlink file")
    if info.st_size > ARCHIVE_MAX_INPUT_BYTES:
        raise ValueError("archive exceeds input size limit")
    archive_sha, hashed_size = _hash_regular_file(path)
    if hashed_size != info.st_size:
        raise RuntimeError("archive changed during inspection")
    return _ArchiveSnapshot(
        path=path,
        sha256=archive_sha,
        dev=info.st_dev,
        ino=info.st_ino,
        size=info.st_size,
        format=_archive_format(path),
    )


def _revalidate_archive(snapshot: _ArchiveSnapshot) -> None:
    info = os.lstat(snapshot.path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("archive changed during operation")
    if (info.st_dev, info.st_ino, info.st_size) != (
        snapshot.dev,
        snapshot.ino,
        snapshot.size,
    ):
        raise RuntimeError("archive changed during operation")
    current_sha, current_size = _hash_regular_file(snapshot.path)
    if current_size != snapshot.size or not secrets.compare_digest(current_sha, snapshot.sha256):
        raise RuntimeError("archive changed during operation")


def _resolve_new_directory(raw_path: str, roots: list[Path]) -> Path:
    raw = Path(raw_path).expanduser()
    if not raw.is_absolute():
        raise PermissionError("destination must be absolute")
    parent = raw.parent.resolve(strict=True)
    if not _inside(parent, roots):
        raise PermissionError("destination is outside configured allowed roots")
    target = parent / raw.name
    if target in roots:
        raise PermissionError("operation on an allowed root itself is not permitted")
    if target.exists() or target.is_symlink():
        raise FileExistsError("destination already exists")
    return target


def _resolve_archive_output(
    raw_path: str,
    roots: list[Path],
    *,
    overwrite: bool,
    expected_sha256: str | None,
) -> tuple[Path, str, bool, int, tuple[int, int] | None]:
    raw = Path(raw_path).expanduser()
    if not raw.is_absolute():
        raise PermissionError("archive output path must be absolute")
    parent = raw.parent.resolve(strict=True)
    if not _inside(parent, roots):
        raise PermissionError("archive output is outside configured allowed roots")
    output = parent / raw.name
    fmt = _archive_format(output)
    if output in roots:
        raise PermissionError("operation on an allowed root itself is not permitted")
    if output.is_symlink():
        raise PermissionError("archive output symlink is not permitted")
    if output.exists():
        if not output.is_file():
            raise PermissionError("archive output must be a regular file")
        if not overwrite:
            raise FileExistsError("archive output already exists")
        if expected_sha256 is None:
            raise ValueError("expected_sha256 is required when overwriting archive output")
        info = os.lstat(output)
        if not secrets.compare_digest(file_sha256(output), expected_sha256):
            raise RuntimeError("archive output changed since read")
        return output, fmt, True, stat.S_IMODE(info.st_mode), (info.st_dev, info.st_ino)
    if expected_sha256 is not None:
        raise ValueError("expected_sha256 requires overwrite=true")
    return output, fmt, False, 0o600, None


def _revalidate_output(
    output: Path,
    *,
    existed: bool,
    expected_sha256: str | None,
    identity: tuple[int, int] | None,
) -> None:
    if existed:
        info = os.lstat(output)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RuntimeError("archive output changed before publication")
        if identity is None or (info.st_dev, info.st_ino) != identity:
            raise RuntimeError("archive output changed before publication")
        if expected_sha256 is None or not secrets.compare_digest(
            file_sha256(output), expected_sha256
        ):
            raise RuntimeError("archive output changed since read")
        return
    if output.exists() or output.is_symlink():
        raise RuntimeError("archive output target appeared during creation")


def _safe_member_path(name: str) -> str:
    if "\x00" in name:
        raise ValueError("archive entry contains NUL")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        raise ValueError("archive contains an absolute entry path")
    pure = PurePosixPath(normalized)
    parts = pure.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("archive contains an unsafe entry path")
    if ":" in parts[0]:
        raise ValueError("archive contains a drive-qualified entry path")
    return pure.as_posix()


def _zip_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_IFMT(mode) == stat.S_IFLNK


def _scan_zip(path: Path, *, list_limit: int) -> tuple[int, int, list[ArchiveEntry], bool]:
    entries = 0
    total = 0
    listed: list[ArchiveEntry] = []
    seen: set[str] = set()
    with zipfile.ZipFile(path, "r") as archive:
        for info in archive.infolist():
            normalized = _safe_member_path(info.filename)
            if normalized in seen:
                raise ValueError("archive contains duplicate entry paths")
            seen.add(normalized)
            if info.flag_bits & 0x1:
                raise ValueError("encrypted ZIP entries are not supported")
            if _zip_is_symlink(info):
                raise ValueError("archive contains a symlink entry")
            entries += 1
            if entries > ARCHIVE_MAX_ENTRIES:
                raise ValueError("archive exceeds entry limit")
            is_dir = info.is_dir()
            if not is_dir:
                total += info.file_size
                if total > ARCHIVE_MAX_TOTAL_BYTES:
                    raise ValueError("archive exceeds uncompressed byte limit")
            if len(listed) < list_limit:
                listed.append(
                    ArchiveEntry(
                        path=normalized, size=0 if is_dir else info.file_size, is_dir=is_dir
                    )
                )
    return entries, total, listed, entries > list_limit


def _scan_tar(path: Path, *, list_limit: int) -> tuple[int, int, list[ArchiveEntry], bool]:
    entries = 0
    total = 0
    listed: list[ArchiveEntry] = []
    seen: set[str] = set()
    with tarfile.open(path, "r:*") as archive:
        for info in archive:
            normalized = _safe_member_path(info.name)
            if normalized in seen:
                raise ValueError("archive contains duplicate entry paths")
            seen.add(normalized)
            if info.issym() or info.islnk() or info.isdev() or info.isfifo():
                raise ValueError("archive contains a link or special entry")
            if not (info.isdir() or info.isfile()):
                raise ValueError("archive contains an unsupported entry type")
            entries += 1
            if entries > ARCHIVE_MAX_ENTRIES:
                raise ValueError("archive exceeds entry limit")
            if info.isfile():
                total += info.size
                if total > ARCHIVE_MAX_TOTAL_BYTES:
                    raise ValueError("archive exceeds uncompressed byte limit")
            if len(listed) < list_limit:
                listed.append(
                    ArchiveEntry(
                        path=normalized, size=info.size if info.isfile() else 0, is_dir=info.isdir()
                    )
                )
    return entries, total, listed, entries > list_limit


def _scan_archive(
    snapshot: _ArchiveSnapshot, *, list_limit: int = ARCHIVE_LIST_LIMIT
) -> tuple[int, int, list[ArchiveEntry], bool]:
    if snapshot.format == "zip":
        return _scan_zip(snapshot.path, list_limit=list_limit)
    return _scan_tar(snapshot.path, list_limit=list_limit)


def _ensure_parent(base: Path, relative: str) -> Path:
    target = base.joinpath(*PurePosixPath(relative).parts)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return target


def _extract_zip(snapshot: _ArchiveSnapshot, temp_dir: Path) -> tuple[int, int]:
    entries = 0
    total = 0
    seen: set[str] = set()
    with zipfile.ZipFile(snapshot.path, "r") as archive:
        for info in archive.infolist():
            relative = _safe_member_path(info.filename)
            if relative in seen:
                raise ValueError("archive contains duplicate entry paths")
            seen.add(relative)
            if info.flag_bits & 0x1 or _zip_is_symlink(info):
                raise ValueError("archive contains unsupported ZIP entry")
            entries += 1
            if entries > ARCHIVE_MAX_ENTRIES:
                raise ValueError("archive exceeds entry limit")
            target = _ensure_parent(temp_dir, relative)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            written = 0
            with archive.open(info, "r") as source, target.open("xb") as output:
                os.chmod(target, 0o600)
                while True:
                    chunk = source.read(ARCHIVE_CHUNK_BYTES)
                    if not chunk:
                        break
                    written += len(chunk)
                    total += len(chunk)
                    if written > info.file_size or total > ARCHIVE_MAX_TOTAL_BYTES:
                        raise ValueError("archive exceeds uncompressed byte limit")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if written != info.file_size:
                raise ValueError("ZIP entry size changed during extraction")
    return entries, total


def _extract_tar(snapshot: _ArchiveSnapshot, temp_dir: Path) -> tuple[int, int]:
    entries = 0
    total = 0
    seen: set[str] = set()
    with tarfile.open(snapshot.path, "r:*") as archive:
        for info in archive:
            relative = _safe_member_path(info.name)
            if relative in seen:
                raise ValueError("archive contains duplicate entry paths")
            seen.add(relative)
            if info.issym() or info.islnk() or info.isdev() or info.isfifo():
                raise ValueError("archive contains a link or special entry")
            if not (info.isdir() or info.isfile()):
                raise ValueError("archive contains an unsupported entry type")
            entries += 1
            if entries > ARCHIVE_MAX_ENTRIES:
                raise ValueError("archive exceeds entry limit")
            target = _ensure_parent(temp_dir, relative)
            if info.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            source = archive.extractfile(info)
            if source is None:
                raise ValueError("archive file entry could not be read")
            written = 0
            with source, target.open("xb") as output:
                os.chmod(target, 0o600)
                while True:
                    chunk = source.read(ARCHIVE_CHUNK_BYTES)
                    if not chunk:
                        break
                    written += len(chunk)
                    total += len(chunk)
                    if written > info.size or total > ARCHIVE_MAX_TOTAL_BYTES:
                        raise ValueError("archive exceeds uncompressed byte limit")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if written != info.size:
                raise ValueError("TAR entry size changed during extraction")
    return entries, total


def _inspect_archive_sync(
    request_id: str, raw_path: str, *, roots: list[Path]
) -> ArchiveInspectResult:
    try:
        snapshot = _resolve_existing_archive(raw_path, roots)
        entries, total, listed, truncated = _scan_archive(snapshot)
        _revalidate_archive(snapshot)
        return ArchiveInspectResult(
            request_id=request_id,
            path=str(snapshot.path),
            format=snapshot.format,
            archive_bytes=snapshot.size,
            entries=entries,
            total_uncompressed_bytes=total,
            sha256=snapshot.sha256,
            listing=listed,
            listing_truncated=truncated,
        )
    except (
        OSError,
        PermissionError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        tarfile.TarError,
    ) as exc:
        return ArchiveInspectResult(
            request_id=request_id, path=raw_path, rejected=True, error=str(exc)
        )


def _extract_archive_sync(
    request_id: str, raw_path: str, raw_destination: str, *, roots: list[Path]
) -> ArchiveExtractResult:
    temp_dir: Path | None = None
    try:
        snapshot = _resolve_existing_archive(raw_path, roots)
        destination = _resolve_new_directory(raw_destination, roots)
        _scan_archive(snapshot, list_limit=0)
        temp_dir = Path(tempfile.mkdtemp(prefix=".remote-mcp-archive-", dir=destination.parent))
        os.chmod(temp_dir, 0o700)
        if snapshot.format == "zip":
            entries, total = _extract_zip(snapshot, temp_dir)
        else:
            entries, total = _extract_tar(snapshot, temp_dir)
        _revalidate_archive(snapshot)
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("archive destination appeared during extraction")
        temp_dir.rename(destination)
        _fsync_directory(destination.parent)
        temp_dir = None
        return ArchiveExtractResult(
            request_id=request_id,
            path=str(snapshot.path),
            destination=str(destination),
            format=snapshot.format,
            entries=entries,
            total_uncompressed_bytes=total,
            archive_sha256=snapshot.sha256,
            changed=True,
        )
    except (
        OSError,
        PermissionError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        tarfile.TarError,
    ) as exc:
        return ArchiveExtractResult(
            request_id=request_id,
            path=raw_path,
            destination=raw_destination,
            rejected=True,
            error=str(exc),
        )
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


def _write_zip(source: Path, temp_path: Path) -> None:
    with zipfile.ZipFile(
        temp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as archive:
        for current, dirs, files in os.walk(source, topdown=True, followlinks=False):
            dirs.sort()
            files.sort()
            current_path = Path(current)
            relative_dir = current_path.relative_to(source)
            if relative_dir != Path("."):
                directory = zipfile.ZipInfo(relative_dir.as_posix().rstrip("/") + "/")
                directory.external_attr = (stat.S_IFDIR | 0o755) << 16
                archive.writestr(directory, b"")
            for name in files:
                child = current_path / name
                relative = child.relative_to(source).as_posix()
                archive.write(child, relative)


def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mode = 0o755 if info.isdir() else 0o644
    return info


def _write_tar(source: Path, temp_path: Path, fmt: str) -> None:
    mode = "w:gz" if fmt == "tar.gz" else "w"
    with tarfile.open(temp_path, mode) as archive:
        for current, dirs, files in os.walk(source, topdown=True, followlinks=False):
            dirs.sort()
            files.sort()
            current_path = Path(current)
            relative_dir = current_path.relative_to(source)
            if relative_dir != Path("."):
                archive.add(
                    current_path,
                    arcname=relative_dir.as_posix(),
                    recursive=False,
                    filter=_tar_filter,
                )
            for name in files:
                child = current_path / name
                archive.add(
                    child,
                    arcname=child.relative_to(source).as_posix(),
                    recursive=False,
                    filter=_tar_filter,
                )


def _create_archive_sync(
    request_id: str,
    raw_source: str,
    raw_output: str,
    *,
    roots: list[Path],
    expected_tree_sha256: str,
    overwrite: bool,
    expected_sha256: str | None,
) -> ArchiveCreateResult:
    temp_path: Path | None = None
    try:
        source = _resolve_tree(raw_source, roots, reject_root=False)
        before = _scan_tree(
            source, max_entries=ARCHIVE_MAX_ENTRIES, max_total_bytes=ARCHIVE_MAX_TOTAL_BYTES
        )
        if not secrets.compare_digest(before.tree_sha256, expected_tree_sha256):
            raise RuntimeError("source tree changed since inspection")
        output, fmt, existed, mode, identity = _resolve_archive_output(
            raw_output, roots, overwrite=overwrite, expected_sha256=expected_sha256
        )
        if output.is_relative_to(source):
            raise PermissionError("archive output must be outside source tree")
        fd, temp_name = tempfile.mkstemp(
            prefix=".remote-mcp-archive-", suffix=".tmp", dir=output.parent
        )
        os.close(fd)
        temp_path = Path(temp_name)
        os.chmod(temp_path, 0o600)
        if fmt == "zip":
            _write_zip(source, temp_path)
        else:
            _write_tar(source, temp_path, fmt)
        if temp_path.stat().st_size > ARCHIVE_MAX_INPUT_BYTES:
            raise ValueError("created archive exceeds size limit")
        after = _scan_tree(
            source, max_entries=ARCHIVE_MAX_ENTRIES, max_total_bytes=ARCHIVE_MAX_TOTAL_BYTES
        )
        if not secrets.compare_digest(after.tree_sha256, before.tree_sha256):
            raise RuntimeError("source tree changed during archive creation")
        # Validate the produced archive before publication.
        temp_snapshot = _ArchiveSnapshot(
            path=temp_path,
            sha256=file_sha256(temp_path),
            dev=os.lstat(temp_path).st_dev,
            ino=os.lstat(temp_path).st_ino,
            size=temp_path.stat().st_size,
            format=fmt,
        )
        archive_entries, archive_total, _, _ = _scan_archive(temp_snapshot, list_limit=0)
        if archive_entries != before.entries or archive_total != before.total_bytes:
            raise RuntimeError("created archive does not match inspected source tree")
        _revalidate_output(
            output,
            existed=existed,
            expected_sha256=expected_sha256,
            identity=identity,
        )
        os.chmod(temp_path, mode)
        with temp_path.open("rb") as handle:
            os.fsync(handle.fileno())
        if existed:
            os.replace(temp_path, output)
        else:
            try:
                os.link(temp_path, output)
            except FileExistsError as exc:
                raise RuntimeError("archive output target appeared during creation") from exc
            temp_path.unlink()
        _fsync_directory(output.parent)
        temp_path = None
        return ArchiveCreateResult(
            request_id=request_id,
            source_path=str(source),
            output_path=str(output),
            format=fmt,
            entries=before.entries,
            total_uncompressed_bytes=before.total_bytes,
            archive_bytes=output.stat().st_size,
            sha256=file_sha256(output),
            changed=True,
        )
    except (
        OSError,
        PermissionError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        tarfile.TarError,
    ) as exc:
        return ArchiveCreateResult(
            request_id=request_id,
            source_path=raw_source,
            output_path=raw_output,
            rejected=True,
            error=str(exc),
        )
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


async def inspect_archive(request_id: str, path: str, *, roots: list[Path]) -> ArchiveInspectResult:
    return await asyncio.to_thread(_inspect_archive_sync, request_id, path, roots=roots)


async def extract_archive(
    request_id: str, path: str, destination: str, *, roots: list[Path]
) -> ArchiveExtractResult:
    return await asyncio.to_thread(
        _extract_archive_sync, request_id, path, destination, roots=roots
    )


async def create_archive(
    request_id: str,
    source_path: str,
    output_path: str,
    *,
    roots: list[Path],
    expected_tree_sha256: str,
    overwrite: bool = False,
    expected_sha256: str | None = None,
) -> ArchiveCreateResult:
    return await asyncio.to_thread(
        _create_archive_sync,
        request_id,
        source_path,
        output_path,
        roots=roots,
        expected_tree_sha256=expected_tree_sha256,
        overwrite=overwrite,
        expected_sha256=expected_sha256,
    )

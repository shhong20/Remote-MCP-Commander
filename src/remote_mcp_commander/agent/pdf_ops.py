from __future__ import annotations

import asyncio
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from remote_mcp_commander.agent.document_ops import DOCUMENT_MAX_INPUT_BYTES
from remote_mcp_commander.agent.file_ops import file_sha256, resolve_allowed_path, secrets_match
from remote_mcp_commander.protocol import PdfComposeResult, PdfPageSource

PDFINFO_PATH = Path("/usr/bin/pdfinfo")
PDFDETACH_PATH = Path("/usr/bin/pdfdetach")
PDFSEPARATE_PATH = Path("/usr/bin/pdfseparate")
PDFUNITE_PATH = Path("/usr/bin/pdfunite")
PDF_COMPOSE_MAX_SOURCES = 16
PDF_COMPOSE_MAX_PAGES = 200
PDF_COMPOSE_MAX_TEMP_BYTES = 67_108_864
PDF_COMPOSE_TIMEOUT_S = 75.0


def _remaining_timeout(deadline: float, cap: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("PDF composition timed out")
    return max(0.1, min(cap, remaining))


@dataclass(frozen=True)
class _PdfSnapshot:
    path: Path
    sha256: str
    identity: tuple[int, int, int, int, int]
    pages: int


def _identity(path: Path) -> tuple[int, int, int, int, int]:
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _require_tool(path: Path, label: str) -> None:
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"{label} is unavailable on this Agent")


def _bounded_error(process: subprocess.CompletedProcess[bytes], fallback: str) -> str:
    text = process.stderr.decode("utf-8", errors="replace").strip()
    return text[:512] or fallback


def _reject_unsafe_pdf_features(path: Path, *, deadline: float) -> None:
    javascript = subprocess.run(
        [str(PDFINFO_PATH), "-js", str(path)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=_remaining_timeout(deadline, 15.0),
        check=False,
    )
    if javascript.returncode != 0:
        raise ValueError(_bounded_error(javascript, "PDF JavaScript inspection failed"))
    if javascript.stdout.strip():
        raise ValueError("PDF JavaScript is not supported for composition")

    attachments = subprocess.run(
        [str(PDFDETACH_PATH), "-list", str(path)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=_remaining_timeout(deadline, 15.0),
        check=False,
    )
    if attachments.returncode != 0:
        raise ValueError(_bounded_error(attachments, "PDF attachment inspection failed"))
    output = attachments.stdout.decode("utf-8", errors="replace")
    match = re.search(r"(?m)^\s*(\d+)\s+embedded files?\s*$", output)
    if match is None:
        raise ValueError("PDF attachment inspection returned an unexpected result")
    if int(match.group(1)):
        raise ValueError("PDF embedded attachments are not supported for composition")


def _pdf_page_count(path: Path, *, timeout_s: float = 15.0) -> int:
    process = subprocess.run(
        [str(PDFINFO_PATH), str(path)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout_s,
        check=False,
    )
    if process.returncode != 0:
        raise ValueError(_bounded_error(process, "PDF metadata extraction failed"))
    output = process.stdout.decode("utf-8", errors="replace")
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().lower() == "pages":
            try:
                pages = int(value.strip())
            except ValueError as exc:
                raise ValueError("PDF page count is invalid") from exc
            if pages < 1:
                raise ValueError("PDF has no pages")
            return pages
    raise ValueError("PDF page count is unavailable")


def _resolve_source(raw_path: str, roots: list[Path], *, deadline: float) -> _PdfSnapshot:
    requested = Path(raw_path).expanduser()
    if not requested.is_absolute():
        raise PermissionError("PDF source path must be absolute")
    if requested.is_symlink():
        raise PermissionError("symlink PDF sources are not supported")
    path = resolve_allowed_path(raw_path, roots).resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise ValueError("PDF source must be a regular .pdf file")
    if path.stat().st_size > DOCUMENT_MAX_INPUT_BYTES:
        raise ValueError("PDF source exceeds size limit")
    sha256 = file_sha256(path)
    _reject_unsafe_pdf_features(path, deadline=deadline)
    return _PdfSnapshot(
        path=path,
        sha256=sha256,
        identity=_identity(path),
        pages=_pdf_page_count(path, timeout_s=_remaining_timeout(deadline, 15.0)),
    )


def _revalidate_source(snapshot: _PdfSnapshot) -> None:
    if snapshot.path.is_symlink():
        raise ValueError("PDF source changed during composition")
    try:
        identity = _identity(snapshot.path)
    except FileNotFoundError as exc:
        raise ValueError("PDF source changed during composition") from exc
    if identity != snapshot.identity or not secrets_match(
        file_sha256(snapshot.path), snapshot.sha256
    ):
        raise ValueError("PDF source changed during composition")


def _resolve_output(
    raw_path: str,
    roots: list[Path],
    *,
    overwrite: bool,
    expected_sha256: str | None,
) -> tuple[Path, bool, int, tuple[int, int, int, int, int] | None]:
    requested = Path(raw_path).expanduser()
    if not requested.is_absolute():
        raise PermissionError("PDF output path must be absolute")
    if requested.suffix.lower() != ".pdf":
        raise ValueError("PDF output path must end with .pdf")
    if requested.is_symlink():
        raise PermissionError("symlink PDF outputs are not supported")
    path = resolve_allowed_path(raw_path, roots)
    parent = path.parent.resolve(strict=True)
    if not parent.is_dir() or not any(
        parent == root or parent.is_relative_to(root) for root in roots
    ):
        raise PermissionError("PDF output parent is outside configured allowed roots")
    path = parent / path.name
    exists = path.exists()
    if exists:
        if not path.is_file():
            raise ValueError("PDF output target is not a regular file")
        if not overwrite:
            raise ValueError("PDF output target already exists")
        if expected_sha256 is None:
            raise ValueError("expected_sha256 is required when overwriting PDF output")
        if not secrets_match(file_sha256(path), expected_sha256):
            raise ValueError("PDF output changed since read")
        return path, True, stat.S_IMODE(path.stat().st_mode) & 0o777, _identity(path)
    if overwrite and expected_sha256 is not None:
        raise ValueError("PDF output does not exist for guarded overwrite")
    return path, False, 0o600, None


def _revalidate_output(
    path: Path,
    *,
    existed: bool,
    expected_sha256: str | None,
    expected_identity: tuple[int, int, int, int, int] | None,
) -> None:
    if not existed:
        if path.exists() or path.is_symlink():
            raise ValueError("PDF output target appeared during composition")
        return
    if path.is_symlink():
        raise ValueError("PDF output changed during composition")
    try:
        identity = _identity(path)
    except FileNotFoundError as exc:
        raise ValueError("PDF output changed during composition") from exc
    if expected_identity is None or identity != expected_identity:
        raise ValueError("PDF output changed during composition")
    if expected_sha256 is None or not secrets_match(file_sha256(path), expected_sha256):
        raise ValueError("PDF output changed during composition")


def _extract_pages(
    snapshot: _PdfSnapshot,
    *,
    start_page: int,
    end_page: int,
    temp_dir: Path,
    source_index: int,
    deadline: float,
    remaining_temp_bytes: int,
) -> tuple[list[Path], int]:
    result: list[Path] = []
    bytes_written = 0
    for page in range(start_page, end_page + 1):
        extracted = temp_dir / f"source-{source_index}-{page}.pdf"
        process = subprocess.run(
            [
                str(PDFSEPARATE_PATH),
                "-f",
                str(page),
                "-l",
                str(page),
                str(snapshot.path),
                str(extracted),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=_remaining_timeout(deadline, 15.0),
            check=False,
        )
        if process.returncode != 0:
            raise ValueError(_bounded_error(process, "PDF page extraction failed"))
        if not extracted.is_file():
            raise ValueError("PDF page extraction produced incomplete output")
        bytes_written += extracted.stat().st_size
        if bytes_written > remaining_temp_bytes:
            raise ValueError("PDF extracted page data exceeds temporary size limit")
        result.append(extracted)
    _revalidate_source(snapshot)
    return result, bytes_written


def _compose_pdf_pages_sync(
    request_id: str,
    output_path: str,
    sources: list[PdfPageSource],
    *,
    roots: list[Path],
    overwrite: bool,
    expected_sha256: str | None,
) -> PdfComposeResult:
    temp_dir: Path | None = None
    deadline = time.monotonic() + PDF_COMPOSE_TIMEOUT_S
    try:
        _require_tool(PDFINFO_PATH, "pdfinfo")
        _require_tool(PDFDETACH_PATH, "pdfdetach")
        _require_tool(PDFSEPARATE_PATH, "pdfseparate")
        _require_tool(PDFUNITE_PATH, "pdfunite")
        if len(sources) > PDF_COMPOSE_MAX_SOURCES:
            raise ValueError(f"PDF composition exceeds {PDF_COMPOSE_MAX_SOURCES} source limit")
        output, existed, mode, output_identity = _resolve_output(
            output_path,
            roots,
            overwrite=overwrite,
            expected_sha256=expected_sha256,
        )

        snapshots: dict[str, _PdfSnapshot] = {}
        resolved_specs: list[tuple[_PdfSnapshot, int, int]] = []
        total_pages = 0
        for source in sources:
            snapshot = snapshots.get(source.path)
            if snapshot is None:
                snapshot = _resolve_source(source.path, roots, deadline=deadline)
                snapshots[source.path] = snapshot
            if snapshot.path == output:
                raise ValueError("PDF output must be different from every source PDF")
            end_page = source.end_page or snapshot.pages
            if source.start_page > end_page:
                raise ValueError("PDF source start_page must not exceed end_page")
            if end_page > snapshot.pages:
                raise ValueError(
                    f"PDF source page range exceeds total pages {snapshot.pages}: {source.path}"
                )
            count = end_page - source.start_page + 1
            total_pages += count
            if total_pages > PDF_COMPOSE_MAX_PAGES:
                raise ValueError(f"PDF composition exceeds {PDF_COMPOSE_MAX_PAGES} page limit")
            resolved_specs.append((snapshot, source.start_page, end_page))

        temp_dir = Path(tempfile.mkdtemp(prefix=".remote-mcp-pdf-", dir=output.parent))
        if os.name != "nt":
            os.chmod(temp_dir, 0o700)
        page_files: list[Path] = []
        temp_bytes = 0
        for index, (snapshot, start_page, end_page) in enumerate(resolved_specs):
            extracted, extracted_bytes = _extract_pages(
                snapshot,
                start_page=start_page,
                end_page=end_page,
                temp_dir=temp_dir,
                source_index=index,
                deadline=deadline,
                remaining_temp_bytes=PDF_COMPOSE_MAX_TEMP_BYTES - temp_bytes,
            )
            page_files.extend(extracted)
            temp_bytes += extracted_bytes

        temp_output = temp_dir / "composed.pdf"
        process = subprocess.run(
            [str(PDFUNITE_PATH), *[str(item) for item in page_files], str(temp_output)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=_remaining_timeout(deadline, 60.0),
            check=False,
        )
        if process.returncode != 0:
            raise ValueError(_bounded_error(process, "PDF page composition failed"))
        if not temp_output.is_file():
            raise ValueError("PDF page composition did not produce an output file")
        if temp_output.stat().st_size > DOCUMENT_MAX_INPUT_BYTES:
            raise ValueError("composed PDF exceeds size limit")
        if _pdf_page_count(
            temp_output, timeout_s=_remaining_timeout(deadline, 15.0)
        ) != total_pages:
            raise ValueError("composed PDF page count does not match requested pages")

        if os.name != "nt":
            os.chmod(temp_output, mode)
        with temp_output.open("rb") as handle:
            os.fsync(handle.fileno())
        for snapshot in snapshots.values():
            _revalidate_source(snapshot)
        _revalidate_output(
            output,
            existed=existed,
            expected_sha256=expected_sha256,
            expected_identity=output_identity,
        )
        if existed:
            os.replace(temp_output, output)
        else:
            try:
                os.link(temp_output, output)
            except FileExistsError as exc:
                raise ValueError("PDF output target appeared during composition") from exc
            temp_output.unlink()
        _fsync_directory(output.parent)
        return PdfComposeResult(
            request_id=request_id,
            output_path=str(output),
            pages_written=total_pages,
            bytes_written=output.stat().st_size,
            sha256=file_sha256(output),
        )
    except (
        OSError,
        PermissionError,
        subprocess.TimeoutExpired,
        ValueError,
    ) as exc:
        return PdfComposeResult(
            request_id=request_id,
            output_path=output_path,
            rejected=True,
            error=str(exc),
        )
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


async def compose_pdf_pages(
    request_id: str,
    output_path: str,
    sources: list[PdfPageSource],
    *,
    roots: list[Path],
    overwrite: bool = False,
    expected_sha256: str | None = None,
) -> PdfComposeResult:
    return await asyncio.to_thread(
        _compose_pdf_pages_sync,
        request_id,
        output_path,
        sources,
        roots=roots,
        overwrite=overwrite,
        expected_sha256=expected_sha256,
    )

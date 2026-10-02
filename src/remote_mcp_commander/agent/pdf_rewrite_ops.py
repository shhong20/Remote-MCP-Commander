from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from remote_mcp_commander.agent.document_ops import DOCUMENT_MAX_INPUT_BYTES
from remote_mcp_commander.agent.file_ops import file_sha256, secrets_match
from remote_mcp_commander.agent.pdf_ops import (
    PDFUNITE_PATH,
    _extract_pages,
    _fsync_directory,
    _pdf_page_count,
    _reject_unsafe_pdf_features,
    _remaining_timeout,
    _require_tool,
    _resolve_output,
    _resolve_source,
    _revalidate_output,
    _revalidate_source,
)
from remote_mcp_commander.agent.pdf_render_ops import (
    PDF_RENDER_MAX_MARKDOWN_BYTES,
    SOFFICE_PATH,
    _render_markdown_to_temp_pdf,
)
from remote_mcp_commander.protocol import (
    PdfMarkdownSegment,
    PdfRewriteResult,
    PdfRewriteSegment,
    PdfSourcePagesSegment,
)

PDF_REWRITE_MAX_SEGMENTS = 32
PDF_REWRITE_MAX_PAGES = 200
PDF_REWRITE_MAX_TEMP_BYTES = 100_663_296
PDF_REWRITE_TIMEOUT_S = 120.0


def _publish_pdf(
    temp_output: Path,
    output: Path,
    *,
    existed: bool,
    mode: int,
    expected_output_sha256: str | None,
    output_identity: tuple[int, int, int, int, int] | None,
) -> None:
    if os.name != "nt":
        os.chmod(temp_output, mode)
    with temp_output.open("rb") as handle:
        os.fsync(handle.fileno())
    _revalidate_output(
        output,
        existed=existed,
        expected_sha256=expected_output_sha256,
        expected_identity=output_identity,
    )
    if existed:
        os.replace(temp_output, output)
    else:
        try:
            os.link(temp_output, output)
        except FileExistsError as exc:
            raise ValueError("PDF output target appeared during rewrite") from exc
        temp_output.unlink()
    _fsync_directory(output.parent)


def _rewrite_pdf_sync(
    request_id: str,
    source_path: str,
    output_path: str,
    segments: list[PdfRewriteSegment],
    *,
    roots: list[Path],
    expected_source_sha256: str,
    overwrite: bool,
    expected_output_sha256: str | None,
) -> PdfRewriteResult:
    temp_dir: Path | None = None
    deadline = time.monotonic() + PDF_REWRITE_TIMEOUT_S
    try:
        _require_tool(PDFUNITE_PATH, "pdfunite")
        if len(segments) > PDF_REWRITE_MAX_SEGMENTS:
            raise ValueError(
                f"PDF rewrite exceeds {PDF_REWRITE_MAX_SEGMENTS} segment limit"
            )
        source = _resolve_source(source_path, roots, deadline=deadline)
        if not secrets_match(source.sha256, expected_source_sha256):
            raise ValueError("PDF source changed since read")
        output, existed, mode, output_identity = _resolve_output(
            output_path,
            roots,
            overwrite=overwrite,
            expected_sha256=expected_output_sha256,
        )
        if source.path == output:
            raise ValueError("PDF rewrite output must be different from source PDF")

        markdown_bytes = sum(
            len(segment.markdown.encode("utf-8"))
            for segment in segments
            if isinstance(segment, PdfMarkdownSegment)
        )
        if markdown_bytes > PDF_RENDER_MAX_MARKDOWN_BYTES:
            raise ValueError("PDF rewrite Markdown input exceeds UTF-8 byte limit")
        if any(isinstance(segment, PdfMarkdownSegment) for segment in segments):
            _require_tool(SOFFICE_PATH, "LibreOffice Writer")

        temp_dir = Path(tempfile.mkdtemp(prefix=".remote-mcp-rewrite-", dir=output.parent))
        if os.name != "nt":
            os.chmod(temp_dir, 0o700)

        pieces: list[Path] = []
        total_pages = 0
        temp_bytes = 0
        for index, segment in enumerate(segments):
            if isinstance(segment, PdfSourcePagesSegment):
                end_page = segment.end_page or source.pages
                if segment.start_page > end_page:
                    raise ValueError("PDF source start_page must not exceed end_page")
                if end_page > source.pages:
                    raise ValueError(
                        f"PDF source page range exceeds total pages {source.pages}"
                    )
                count = end_page - segment.start_page + 1
                if total_pages + count > PDF_REWRITE_MAX_PAGES:
                    raise ValueError(
                        f"PDF rewrite exceeds {PDF_REWRITE_MAX_PAGES} page limit"
                    )
                extracted, extracted_bytes = _extract_pages(
                    source,
                    start_page=segment.start_page,
                    end_page=end_page,
                    temp_dir=temp_dir,
                    source_index=index,
                    deadline=deadline,
                    remaining_temp_bytes=PDF_REWRITE_MAX_TEMP_BYTES - temp_bytes,
                )
                pieces.extend(extracted)
                total_pages += count
                temp_bytes += extracted_bytes
                continue

            rendered, pages = _render_markdown_to_temp_pdf(
                segment.markdown,
                temp_dir,
                deadline=deadline,
                stem=f"markdown-{index}",
            )
            if total_pages + pages > PDF_REWRITE_MAX_PAGES:
                raise ValueError(
                    f"PDF rewrite exceeds {PDF_REWRITE_MAX_PAGES} page limit"
                )
            rendered_bytes = rendered.stat().st_size
            if temp_bytes + rendered_bytes > PDF_REWRITE_MAX_TEMP_BYTES:
                raise ValueError("PDF rewrite temporary data exceeds size limit")
            pieces.append(rendered)
            total_pages += pages
            temp_bytes += rendered_bytes

        if not pieces or total_pages < 1:
            raise ValueError("PDF rewrite produced no pages")
        _revalidate_source(source)
        if not secrets_match(source.sha256, expected_source_sha256):
            raise ValueError("PDF source changed since read")

        temp_output = temp_dir / "rewritten.pdf"
        process = subprocess.run(
            [str(PDFUNITE_PATH), *[str(piece) for piece in pieces], str(temp_output)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_remaining_timeout(deadline, 75.0),
            check=False,
        )
        if process.returncode != 0:
            error = process.stderr.decode("utf-8", errors="replace").strip()
            raise ValueError(error[:512] or "PDF rewrite composition failed")
        if not temp_output.is_file():
            raise ValueError("PDF rewrite did not produce an output file")
        if temp_output.stat().st_size > DOCUMENT_MAX_INPUT_BYTES:
            raise ValueError("rewritten PDF exceeds size limit")
        pages_written = _pdf_page_count(
            temp_output, timeout_s=_remaining_timeout(deadline, 15.0)
        )
        if pages_written != total_pages:
            raise ValueError("rewritten PDF page count does not match requested sequence")
        _reject_unsafe_pdf_features(temp_output, deadline=deadline)
        _revalidate_source(source)
        if not secrets_match(source.sha256, expected_source_sha256):
            raise ValueError("PDF source changed since read")
        _publish_pdf(
            temp_output,
            output,
            existed=existed,
            mode=mode,
            expected_output_sha256=expected_output_sha256,
            output_identity=output_identity,
        )
        return PdfRewriteResult(
            request_id=request_id,
            output_path=str(output),
            pages_written=pages_written,
            bytes_written=output.stat().st_size,
            sha256=file_sha256(output),
        )
    except (
        OSError,
        PermissionError,
        subprocess.TimeoutExpired,
        ValueError,
    ) as exc:
        return PdfRewriteResult(
            request_id=request_id,
            output_path=output_path,
            rejected=True,
            error=str(exc),
        )
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


async def rewrite_pdf_with_markdown(
    request_id: str,
    source_path: str,
    output_path: str,
    segments: list[PdfRewriteSegment],
    *,
    roots: list[Path],
    expected_source_sha256: str,
    overwrite: bool = False,
    expected_output_sha256: str | None = None,
) -> PdfRewriteResult:
    return await asyncio.to_thread(
        _rewrite_pdf_sync,
        request_id,
        source_path,
        output_path,
        segments,
        roots=roots,
        expected_source_sha256=expected_source_sha256,
        overwrite=overwrite,
        expected_output_sha256=expected_output_sha256,
    )

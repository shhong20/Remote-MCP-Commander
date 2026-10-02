from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from remote_mcp_commander.agent import pdf_rewrite_ops
from remote_mcp_commander.agent.pdf_render_ops import render_pdf_from_markdown
from remote_mcp_commander.agent.pdf_rewrite_ops import rewrite_pdf_with_markdown
from remote_mcp_commander.protocol import (
    PdfMarkdownSegment,
    PdfRewriteBody,
    PdfSourcePagesSegment,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def _make_source(path: Path, roots: list[Path]) -> str:
    markdown = (
        "# SOURCE ONE\n\n"
        "<!-- pagebreak -->\n\n"
        "# SOURCE TWO\n\n"
        "<!-- pagebreak -->\n\n"
        "# SOURCE THREE\n\n"
        "<!-- pagebreak -->\n\n"
        "# SOURCE FOUR\n\n"
        "<!-- pagebreak -->\n\n"
        "# SOURCE FIVE\n"
    )
    result = await render_pdf_from_markdown("setup", str(path), markdown, roots=roots)
    assert result.rejected is False, result.error
    assert result.pages_written == 5
    assert result.sha256 is not None
    return result.sha256


def _pdf_text_pages(path: Path) -> list[str]:
    text = subprocess.run(
        ["/usr/bin/pdftotext", str(path), "-"],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", errors="replace")
    return [page.strip() for page in text.split("\f") if page.strip()]


@pytest.mark.asyncio
async def test_rewrite_pdf_reorders_deletes_and_inserts_markdown(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    output = tmp_path / "rewritten.pdf"
    source_sha = await _make_source(source, [tmp_path])

    result = await rewrite_pdf_with_markdown(
        "req",
        str(source),
        str(output),
        [
            PdfSourcePagesSegment(start_page=1, end_page=2),
            PdfMarkdownSegment(markdown="# INSERTED PAGE\n\nInserted body."),
            PdfSourcePagesSegment(start_page=5, end_page=5),
            PdfSourcePagesSegment(start_page=3, end_page=4),
        ],
        roots=[tmp_path],
        expected_source_sha256=source_sha,
    )

    assert result.rejected is False, result.error
    assert result.pages_written == 6
    assert result.sha256 == _sha256(output)
    assert _sha256(source) == source_sha
    pages = _pdf_text_pages(output)
    assert len(pages) == 6
    assert "SOURCE ONE" in pages[0]
    assert "SOURCE TWO" in pages[1]
    assert "INSERTED PAGE" in pages[2]
    assert "SOURCE FIVE" in pages[3]
    assert "SOURCE THREE" in pages[4]
    assert "SOURCE FOUR" in pages[5]
    assert not list(tmp_path.glob(".remote-mcp-rewrite-*"))


@pytest.mark.asyncio
async def test_rewrite_pdf_requires_current_source_sha(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    output = tmp_path / "rewritten.pdf"
    await _make_source(source, [tmp_path])

    result = await rewrite_pdf_with_markdown(
        "req",
        str(source),
        str(output),
        [PdfSourcePagesSegment(start_page=1, end_page=1)],
        roots=[tmp_path],
        expected_source_sha256="0" * 64,
    )

    assert result.rejected is True
    assert "source changed since read" in (result.error or "")
    assert not output.exists()


@pytest.mark.asyncio
async def test_rewrite_pdf_rejects_source_as_output(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source_sha = await _make_source(source, [tmp_path])

    result = await rewrite_pdf_with_markdown(
        "req",
        str(source),
        str(source),
        [PdfSourcePagesSegment(start_page=1, end_page=2)],
        roots=[tmp_path],
        expected_source_sha256=source_sha,
        overwrite=True,
        expected_output_sha256=source_sha,
    )

    assert result.rejected is True
    assert "different from source" in (result.error or "")
    assert _sha256(source) == source_sha


@pytest.mark.asyncio
async def test_rewrite_pdf_requires_current_output_sha_for_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    output = tmp_path / "output.pdf"
    source_sha = await _make_source(source, [tmp_path])
    output_sha = await _make_source(output, [tmp_path])

    stale = await rewrite_pdf_with_markdown(
        "stale",
        str(source),
        str(output),
        [PdfSourcePagesSegment(start_page=2, end_page=2)],
        roots=[tmp_path],
        expected_source_sha256=source_sha,
        overwrite=True,
        expected_output_sha256="0" * 64,
    )
    assert stale.rejected is True
    assert "changed since read" in (stale.error or "")
    assert _sha256(output) == output_sha

    accepted = await rewrite_pdf_with_markdown(
        "accepted",
        str(source),
        str(output),
        [PdfSourcePagesSegment(start_page=2, end_page=2)],
        roots=[tmp_path],
        expected_source_sha256=source_sha,
        overwrite=True,
        expected_output_sha256=output_sha,
    )
    assert accepted.rejected is False, accepted.error
    assert accepted.pages_written == 1
    assert "SOURCE TWO" in _pdf_text_pages(output)[0]


@pytest.mark.asyncio
async def test_rewrite_pdf_rejects_page_range_beyond_source(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    output = tmp_path / "output.pdf"
    source_sha = await _make_source(source, [tmp_path])

    result = await rewrite_pdf_with_markdown(
        "req",
        str(source),
        str(output),
        [PdfSourcePagesSegment(start_page=5, end_page=6)],
        roots=[tmp_path],
        expected_source_sha256=source_sha,
    )

    assert result.rejected is True
    assert "exceeds total pages 5" in (result.error or "")
    assert not output.exists()


@pytest.mark.asyncio
async def test_rewrite_pdf_detects_source_change_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    output = tmp_path / "output.pdf"
    source_sha = await _make_source(source, [tmp_path])
    original_extract = pdf_rewrite_ops._extract_pages

    def racing_extract(*args, **kwargs):
        result = original_extract(*args, **kwargs)
        source.write_bytes(source.read_bytes() + b"\n% concurrent change\n")
        return result

    monkeypatch.setattr(pdf_rewrite_ops, "_extract_pages", racing_extract)
    result = await rewrite_pdf_with_markdown(
        "req",
        str(source),
        str(output),
        [PdfSourcePagesSegment(start_page=1, end_page=1)],
        roots=[tmp_path],
        expected_source_sha256=source_sha,
    )

    assert result.rejected is True
    assert "source changed" in (result.error or "")
    assert not output.exists()
    assert not list(tmp_path.glob(".remote-mcp-rewrite-*"))


def test_pdf_rewrite_contract_requires_source_segment() -> None:
    with pytest.raises(ValueError, match="source_pages"):
        PdfRewriteBody(
            source_path="/srv/source.pdf",
            output_path="/srv/output.pdf",
            segments=[PdfMarkdownSegment(markdown="# replacement")],
            expected_source_sha256="a" * 64,
        )


def test_pdf_rewrite_contract_bounds_total_markdown_utf8_bytes() -> None:
    oversized = "😀" * 65_537
    with pytest.raises(ValueError, match="UTF-8 byte limit"):
        PdfRewriteBody(
            source_path="/srv/source.pdf",
            output_path="/srv/output.pdf",
            segments=[
                PdfSourcePagesSegment(start_page=1, end_page=1),
                PdfMarkdownSegment(markdown=oversized[:32_768]),
                PdfMarkdownSegment(markdown=oversized[32_768:]),
            ],
            expected_source_sha256="a" * 64,
        )

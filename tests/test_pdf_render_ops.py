from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from remote_mcp_commander.agent import pdf_render_ops
from remote_mcp_commander.agent.pdf_render_ops import (
    _build_fodt,
    render_pdf_from_markdown,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_fodt_escapes_raw_html_and_supports_safe_inline_markup() -> None:
    fodt = _build_fodt(
        "# 제목\n\n<script>alert(1)</script> **굵게** *기울임* `코드`"
    )

    assert "<script>" not in fodt
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in fodt
    assert 'text:style-name="Bold"' in fodt
    assert 'text:style-name="Italic"' in fodt
    assert 'text:style-name="InlineCode"' in fodt


def test_fodt_rejects_unclosed_code_fence() -> None:
    with pytest.raises(ValueError, match="not closed"):
        _build_fodt("```python\nprint('x')")


@pytest.mark.asyncio
async def test_render_pdf_from_markdown_writes_korean_and_page_break(tmp_path: Path) -> None:
    output = tmp_path / "result.pdf"
    markdown = (
        "# 원격 MCP PDF\n\n"
        "안전한 **한글** 렌더링입니다.\n\n"
        "- 첫 번째 항목\n"
        "- 두 번째 항목\n\n"
        '<div style="page-break-before: always;"></div>\n\n'
        "## 두 번째 페이지\n\n`inline code`와 본문입니다."
    )

    result = await render_pdf_from_markdown(
        "req",
        str(output),
        markdown,
        roots=[tmp_path],
    )

    assert result.rejected is False, result.error
    assert result.pages_written == 2
    assert result.sha256 == _sha256(output)
    assert not list(tmp_path.glob(".remote-mcp-render-*"))

    extracted = subprocess.run(
        ["/usr/bin/pdftotext", str(output), "-"],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", errors="replace")
    normalized = " ".join(extracted.split())
    assert "원격 MCP PDF" in normalized
    assert "두 번째 페이지" in normalized


@pytest.mark.asyncio
async def test_render_pdf_requires_current_overwrite_sha(tmp_path: Path) -> None:
    output = tmp_path / "result.pdf"
    first = await render_pdf_from_markdown(
        "req1",
        str(output),
        "# First\n\nbody",
        roots=[tmp_path],
    )
    assert first.rejected is False
    original_hash = _sha256(output)

    stale = await render_pdf_from_markdown(
        "req2",
        str(output),
        "# Second",
        roots=[tmp_path],
        overwrite=True,
        expected_sha256="0" * 64,
    )
    assert stale.rejected is True
    assert "changed since read" in (stale.error or "")
    assert _sha256(output) == original_hash

    accepted = await render_pdf_from_markdown(
        "req3",
        str(output),
        "# Second",
        roots=[tmp_path],
        overwrite=True,
        expected_sha256=original_hash,
    )
    assert accepted.rejected is False
    assert accepted.sha256 == _sha256(output)
    assert accepted.sha256 != original_hash


@pytest.mark.asyncio
async def test_render_pdf_rejects_oversized_utf8_markdown(tmp_path: Path) -> None:
    output = tmp_path / "result.pdf"
    markdown = "한" * (pdf_render_ops.PDF_RENDER_MAX_MARKDOWN_BYTES // 2)

    result = await render_pdf_from_markdown(
        "req",
        str(output),
        markdown,
        roots=[tmp_path],
    )

    assert result.rejected is True
    assert "UTF-8 byte limit" in (result.error or "")
    assert not output.exists()


@pytest.mark.asyncio
async def test_render_pdf_concurrent_profiles_do_not_conflict(tmp_path: Path) -> None:
    first_path = tmp_path / "first.pdf"
    second_path = tmp_path / "second.pdf"

    first, second = await __import__("asyncio").gather(
        render_pdf_from_markdown(
            "req-a",
            str(first_path),
            "# 첫 번째\n\n동시 렌더링 A",
            roots=[tmp_path],
        ),
        render_pdf_from_markdown(
            "req-b",
            str(second_path),
            "# 두 번째\n\n동시 렌더링 B",
            roots=[tmp_path],
        ),
    )

    assert first.rejected is False, first.error
    assert second.rejected is False, second.error
    assert first_path.is_file()
    assert second_path.is_file()
    assert first.sha256 != second.sha256
    assert not list(tmp_path.glob(".remote-mcp-render-*"))

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path

import pytest

from remote_mcp_commander.agent import pdf_ops
from remote_mcp_commander.agent.pdf_ops import compose_pdf_pages
from remote_mcp_commander.protocol import PdfPageSource


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_pdf(path: Path, pages: int) -> None:
    font_object = 3 + pages * 2
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
    }
    kids = " ".join(f"{3 + index * 2} 0 R" for index in range(pages))
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode()
    for index in range(pages):
        page_object = 3 + index * 2
        content_object = page_object + 1
        text = f"Page {index + 1}"
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects[page_object] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_object} 0 R >> >> "
            f"/Contents {content_object} 0 R >>"
        ).encode()
        objects[content_object] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode()
            + stream
            + b"\nendstream"
        )
    objects[font_object] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (font_object + 1)
    for number in range(1, font_object + 1):
        offsets[number] = len(data)
        data.extend(f"{number} 0 obj\n".encode())
        data.extend(objects[number])
        data.extend(b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {font_object + 1}\n".encode())
    data.extend(b"0000000000 65535 f \n")
    for number in range(1, font_object + 1):
        data.extend(f"{offsets[number]:010d} 00000 n \n".encode())
    data.extend(
        f"trailer\n<< /Size {font_object + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    path.write_bytes(data)


@pytest.mark.asyncio
async def test_compose_pdf_pages_selects_ranges_and_preserves_sources(tmp_path: Path) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    output = tmp_path / "composed.pdf"
    _make_pdf(first, 3)
    _make_pdf(second, 2)
    first_hash = _sha256(first)
    second_hash = _sha256(second)

    result = await compose_pdf_pages(
        "req",
        str(output),
        [
            PdfPageSource(path=str(first), start_page=2, end_page=3),
            PdfPageSource(path=str(second), start_page=1, end_page=1),
        ],
        roots=[tmp_path],
    )

    assert result.rejected is False
    assert result.pages_written == 3
    assert result.sha256 == _sha256(output)
    assert pdf_ops._pdf_page_count(output) == 3
    assert _sha256(first) == first_hash
    assert _sha256(second) == second_hash
    assert not list(tmp_path.glob(".remote-mcp-pdf-*"))


@pytest.mark.asyncio
async def test_compose_pdf_rejects_source_as_output(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _make_pdf(source, 2)
    original_hash = _sha256(source)

    result = await compose_pdf_pages(
        "req",
        str(source),
        [PdfPageSource(path=str(source), start_page=1, end_page=1)],
        roots=[tmp_path],
        overwrite=True,
        expected_sha256=original_hash,
    )

    assert result.rejected is True
    assert "different from every source" in (result.error or "")
    assert _sha256(source) == original_hash


@pytest.mark.asyncio
async def test_compose_pdf_rejects_page_range_beyond_source(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    output = tmp_path / "output.pdf"
    _make_pdf(source, 2)

    result = await compose_pdf_pages(
        "req",
        str(output),
        [PdfPageSource(path=str(source), start_page=2, end_page=3)],
        roots=[tmp_path],
    )

    assert result.rejected is True
    assert "exceeds total pages 2" in (result.error or "")
    assert not output.exists()


@pytest.mark.asyncio
async def test_compose_pdf_requires_current_overwrite_sha(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    output = tmp_path / "output.pdf"
    _make_pdf(source, 2)
    _make_pdf(output, 1)
    original_output_hash = _sha256(output)

    stale = await compose_pdf_pages(
        "req",
        str(output),
        [PdfPageSource(path=str(source), start_page=1, end_page=2)],
        roots=[tmp_path],
        overwrite=True,
        expected_sha256="0" * 64,
    )
    assert stale.rejected is True
    assert "changed since read" in (stale.error or "")
    assert _sha256(output) == original_output_hash

    accepted = await compose_pdf_pages(
        "req2",
        str(output),
        [PdfPageSource(path=str(source), start_page=1, end_page=2)],
        roots=[tmp_path],
        overwrite=True,
        expected_sha256=original_output_hash,
    )
    assert accepted.rejected is False
    assert accepted.pages_written == 2
    assert pdf_ops._pdf_page_count(output) == 2


@pytest.mark.asyncio
async def test_compose_pdf_detects_source_change_during_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    output = tmp_path / "output.pdf"
    _make_pdf(source, 2)
    original_extract = pdf_ops._extract_pages

    def racing_extract(*args, **kwargs):
        result = original_extract(*args, **kwargs)
        source.write_bytes(source.read_bytes() + b"\n% external change\n")
        return result

    monkeypatch.setattr(pdf_ops, "_extract_pages", racing_extract)
    result = await compose_pdf_pages(
        "req",
        str(output),
        [PdfPageSource(path=str(source), start_page=1, end_page=1)],
        roots=[tmp_path],
    )

    assert result.rejected is True
    assert "source changed" in (result.error or "")
    assert not output.exists()
    assert not list(tmp_path.glob(".remote-mcp-pdf-*"))


@pytest.mark.asyncio
async def test_compose_pdf_rejects_source_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    link = tmp_path / "link.pdf"
    output = tmp_path / "output.pdf"
    _make_pdf(source, 1)
    link.symlink_to(source)

    result = await compose_pdf_pages(
        "req",
        str(output),
        [PdfPageSource(path=str(link))],
        roots=[tmp_path],
    )

    assert result.rejected is True
    assert "symlink" in (result.error or "")
    assert not output.exists()


@pytest.mark.asyncio
async def test_compose_pdf_preserves_overwrite_mode(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX mode test")
    source = tmp_path / "source.pdf"
    output = tmp_path / "output.pdf"
    _make_pdf(source, 1)
    _make_pdf(output, 1)
    output.chmod(0o640)
    before = _sha256(output)

    result = await compose_pdf_pages(
        "req",
        str(output),
        [PdfPageSource(path=str(source))],
        roots=[tmp_path],
        overwrite=True,
        expected_sha256=before,
    )

    assert result.rejected is False
    assert output.stat().st_mode & 0o777 == 0o640


@pytest.mark.asyncio
async def test_pdf_compose_rejects_javascript_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "source.pdf"
    _make_pdf(path, 1)

    def fake_run(args, **kwargs):
        if args[1] == "-js":
            return subprocess.CompletedProcess(args, 0, b"app.alert('x')\n", b"")
        raise AssertionError(args)

    monkeypatch.setattr(pdf_ops.subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="JavaScript"):
        pdf_ops._reject_unsafe_pdf_features(path, deadline=time.monotonic() + 5)


def test_pdf_compose_rejects_embedded_attachments_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "source.pdf"
    _make_pdf(path, 1)

    def fake_run(args, **kwargs):
        if args[1] == "-js":
            return subprocess.CompletedProcess(args, 0, b"", b"")
        if args[1] == "-list":
            return subprocess.CompletedProcess(args, 0, b"1 embedded files\n1: payload.bin\n", b"")
        raise AssertionError(args)

    monkeypatch.setattr(pdf_ops.subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="embedded attachments"):
        pdf_ops._reject_unsafe_pdf_features(path, deadline=time.monotonic() + 5)


def test_pdf_extract_enforces_temp_budget_per_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    snapshot = pdf_ops._PdfSnapshot(
        path=source,
        sha256=_sha256(source),
        identity=pdf_ops._identity(source),
        pages=2,
    )
    calls = 0

    def fake_run(args, **kwargs):
        nonlocal calls
        calls += 1
        Path(args[-1]).write_bytes(b"x" * 10)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(pdf_ops.subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="temporary size limit"):
        pdf_ops._extract_pages(
            snapshot,
            start_page=1,
            end_page=2,
            temp_dir=tmp_path,
            source_index=0,
            deadline=time.monotonic() + 5,
            remaining_temp_bytes=5,
        )
    assert calls == 1

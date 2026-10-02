from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import pytest

from remote_mcp_commander.agent import document_ops

WORD_DOCUMENT = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Hello DOCX</w:t></w:r></w:p>
    <w:tbl><w:tr><w:tc><w:p><w:r><w:t>A1</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>B1</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
  </w:body>
</w:document>
"""

WORKBOOK = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets>
</workbook>
"""

WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="worksheet"/>
</Relationships>
"""

SHARED_STRINGS = """<?xml version="1.0" encoding="UTF-8"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <si><t>Name</t></si><si><t>Alice</t></si>
</sst>
"""

SHEET = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>7</v></c></row>
    <row r="2"><c r="A2" t="s"><v>1</v></c><c r="B2" t="b"><v>1</v></c></row>
  </sheetData>
</worksheet>
"""


def make_docx(path: Path) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", WORD_DOCUMENT)


def make_xlsx(path: Path) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/workbook.xml", WORKBOOK)
        zf.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS)
        zf.writestr("xl/sharedStrings.xml", SHARED_STRINGS)
        zf.writestr("xl/worksheets/sheet1.xml", SHEET)


@pytest.mark.asyncio
async def test_preview_docx_extracts_paragraphs_and_tables(tmp_path: Path) -> None:
    path = tmp_path / "sample.docx"
    make_docx(path)
    result = await document_ops.preview_document("req", str(path), roots=[tmp_path])
    assert result.rejected is False
    assert result.kind == "docx"
    assert "Hello DOCX" in result.content
    assert "A1\tB1" in result.content
    assert result.sha256 is not None


@pytest.mark.asyncio
async def test_preview_xlsx_lists_sheets_and_returns_tsv(tmp_path: Path) -> None:
    path = tmp_path / "sample.xlsx"
    make_xlsx(path)
    result = await document_ops.preview_document("req", str(path), roots=[tmp_path])
    assert result.rejected is False
    assert result.kind == "xlsx"
    assert result.sheets == ["Data"]
    assert result.sheet == "Data"
    assert result.rows_returned == 2
    assert result.content == "Name\t7\nAlice\tTRUE\n"


@pytest.mark.asyncio
async def test_preview_xlsx_rejects_unknown_sheet(tmp_path: Path) -> None:
    path = tmp_path / "sample.xlsx"
    make_xlsx(path)
    result = await document_ops.preview_document(
        "req", str(path), roots=[tmp_path], sheet="Missing"
    )
    assert result.rejected is True
    assert "worksheet not found" in (result.error or "")


@pytest.mark.asyncio
async def test_preview_rejects_symlink_document(tmp_path: Path) -> None:
    target = tmp_path / "sample.docx"
    make_docx(target)
    link = tmp_path / "linked.docx"
    link.symlink_to(target)
    result = await document_ops.preview_document("req", str(link), roots=[tmp_path])
    assert result.rejected is True
    assert "symlink" in (result.error or "")


def test_pdf_preview_reports_page_navigation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-placeholder")
    monkeypatch.setattr(document_ops, "PDFTOTEXT_PATH", Path("/bin/true"))
    monkeypatch.setattr(document_ops, "PDFINFO_PATH", Path("/bin/true"))
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        if len(argv) == 2:
            return subprocess.CompletedProcess(argv, 0, stdout=b"Pages:          8\n", stderr=b"")
        return subprocess.CompletedProcess(argv, 0, stdout=b"PDF text\n", stderr=b"")

    monkeypatch.setattr(document_ops.subprocess, "run", fake_run)
    content, truncated, total, returned, next_page = document_ops._pdf_preview(
        path, page=3, max_pages=2, max_chars=100
    )
    assert content == "PDF text\n"
    assert truncated is False
    assert total == 8
    assert returned == 2
    assert next_page == 5
    assert calls[0] == ["/bin/true", str(path)]
    assert calls[1][:5] == ["/bin/true", "-f", "3", "-l", "4"]
    assert calls[1][-2:] == [str(path), "-"]


@pytest.mark.asyncio
async def test_preview_rejects_unsupported_extension(tmp_path: Path) -> None:
    path = tmp_path / "sample.bin"
    path.write_bytes(b"data")
    result = await document_ops.preview_document("req", str(path), roots=[tmp_path])
    assert result.rejected is True
    assert "unsupported document type" in (result.error or "")


@pytest.mark.asyncio
async def test_preview_docx_rejects_dtd(tmp_path: Path) -> None:
    path = tmp_path / "evil.docx"
    xml = b'<!DOCTYPE x [<!ENTITY boom "boom">]><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>&boom;</w:t></w:r></w:p></w:body></w:document>'
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", xml)
    result = await document_ops.preview_document("req", str(path), roots=[tmp_path])
    assert result.rejected is True
    assert "DTD/entity" in (result.error or "")


STRUCTURED_WORD_DOCUMENT = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="H1"/></w:pPr><w:r><w:t>Overview</w:t></w:r></w:p>
    <w:p><w:r><w:t>Body text</w:t></w:r></w:p>
    <w:p><w:pPr><w:sectPr/></w:pPr></w:p>
    <w:p><w:pPr><w:pStyle w:val="H2"/></w:pPr><w:r><w:t>Details</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""

WORD_STYLES = """<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="H1"><w:name w:val="Heading 1"/></w:style>
  <w:style w:type="paragraph" w:styleId="H2"><w:name w:val="Heading 2"/></w:style>
</w:styles>
"""


def make_structured_docx(path: Path) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", STRUCTURED_WORD_DOCUMENT)
        zf.writestr("word/styles.xml", WORD_STYLES)


@pytest.mark.asyncio
async def test_preview_docx_preserves_heading_and_section_structure(tmp_path: Path) -> None:
    path = tmp_path / "structured.docx"
    make_structured_docx(path)
    result = await document_ops.preview_document("req", str(path), roots=[tmp_path])
    assert result.rejected is False
    assert [(heading.level, heading.text) for heading in result.headings] == [
        (1, "Overview"),
        (2, "Details"),
    ]
    assert result.section_breaks == 1
    assert "# Overview\n" in result.content
    assert "## Details\n" in result.content
    assert "\n---\n" in result.content


@pytest.mark.asyncio
async def test_preview_xlsx_supports_normalized_cell_range(tmp_path: Path) -> None:
    path = tmp_path / "sample.xlsx"
    make_xlsx(path)
    result = await document_ops.preview_document(
        "req", str(path), roots=[tmp_path], cell_range="b1:b2"
    )
    assert result.rejected is False
    assert result.cell_range == "B1:B2"
    assert result.rows_returned == 2
    assert result.content == "7\nTRUE\n"


@pytest.mark.asyncio
async def test_preview_xlsx_range_preserves_blank_rows_and_bounds_output(tmp_path: Path) -> None:
    path = tmp_path / "sample.xlsx"
    make_xlsx(path)
    result = await document_ops.preview_document(
        "req", str(path), roots=[tmp_path], cell_range="A1:B4", max_rows=3
    )
    assert result.rejected is False
    assert result.cell_range == "A1:B4"
    assert result.rows_returned == 3
    assert result.content == "Name\t7\nAlice\tTRUE\n\t\n"
    assert result.truncated is True


@pytest.mark.asyncio
async def test_preview_xlsx_rejects_reversed_range(tmp_path: Path) -> None:
    path = tmp_path / "sample.xlsx"
    make_xlsx(path)
    result = await document_ops.preview_document(
        "req", str(path), roots=[tmp_path], cell_range="B2:A1"
    )
    assert result.rejected is True
    assert "start must not be after" in (result.error or "")


def test_pdf_preview_rejects_page_beyond_total(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-placeholder")
    monkeypatch.setattr(document_ops, "PDFTOTEXT_PATH", Path("/bin/true"))
    monkeypatch.setattr(document_ops, "PDFINFO_PATH", Path("/bin/true"))

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, stdout=b"Pages: 2\n", stderr=b"")

    monkeypatch.setattr(document_ops.subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="exceeds total pages 2"):
        document_ops._pdf_preview(path, page=3, max_pages=1, max_chars=100)

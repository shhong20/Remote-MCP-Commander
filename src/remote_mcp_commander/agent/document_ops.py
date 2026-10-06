from __future__ import annotations

import asyncio
import hashlib
import os
import re
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from remote_mcp_commander.agent.file_ops import resolve_allowed_path
from remote_mcp_commander.protocol import DocumentHeading, DocumentPreviewResult

PDFTOTEXT_PATH = Path("/usr/bin/pdftotext")
PDFINFO_PATH = Path("/usr/bin/pdfinfo")
DOCUMENT_MAX_INPUT_BYTES = 33_554_432
DOCUMENT_MAX_ZIP_ENTRIES = 4096
DOCUMENT_MAX_UNCOMPRESSED_BYTES = 67_108_864
DOCUMENT_MAX_XML_BYTES = 16_777_216
XLSX_MAX_COLUMNS = 256
XLSX_MAX_ROWS = 1_048_576

_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _append_bounded(parts: list[str], text: str, *, max_chars: int) -> bool:
    current = sum(len(part) for part in parts)
    remaining = max_chars - current
    if remaining <= 0:
        return True
    if len(text) <= remaining:
        parts.append(text)
        return False
    parts.append(text[:remaining])
    return True


def _validate_zip(zf: zipfile.ZipFile) -> None:
    infos = zf.infolist()
    if len(infos) > DOCUMENT_MAX_ZIP_ENTRIES:
        raise ValueError("document archive has too many entries")
    seen: set[str] = set()
    total = 0
    for info in infos:
        if info.filename in seen:
            raise ValueError("document archive contains duplicate entries")
        seen.add(info.filename)
        if info.flag_bits & 0x1:
            raise ValueError("encrypted document archives are not supported")
        total += info.file_size
        if total > DOCUMENT_MAX_UNCOMPRESSED_BYTES:
            raise ValueError("document archive exceeds uncompressed size limit")


def _read_zip_member(zf: zipfile.ZipFile, name: str, *, required: bool = True) -> bytes | None:
    try:
        info = zf.getinfo(name)
    except KeyError:
        if required:
            raise ValueError(f"document member is missing: {name}") from None
        return None
    if info.file_size > DOCUMENT_MAX_XML_BYTES:
        raise ValueError(f"document XML member exceeds size limit: {name}")
    data = zf.read(info)
    if len(data) != info.file_size:
        raise ValueError(f"document member size changed while reading: {name}")
    return data


def _parse_xml(raw: bytes) -> ElementTree.Element:
    upper = raw.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("document XML DTD/entity declarations are not supported")
    return ElementTree.fromstring(raw)


def _docx_heading_level(value: str) -> int | None:
    normalized = re.sub(r"[\s_-]+", "", value).lower()
    match = re.fullmatch(r"heading([1-9])", normalized)
    return int(match.group(1)) if match else None


def _docx_heading_styles(zf: zipfile.ZipFile) -> dict[str, int]:
    raw = _read_zip_member(zf, "word/styles.xml", required=False)
    if raw is None:
        return {}
    root = _parse_xml(raw)
    result: dict[str, int] = {}
    for style in root.findall(f"{{{_WORD_NS}}}style"):
        if style.attrib.get(f"{{{_WORD_NS}}}type") != "paragraph":
            continue
        style_id = style.attrib.get(f"{{{_WORD_NS}}}styleId", "")
        if not style_id:
            continue
        name_node = style.find(f"{{{_WORD_NS}}}name")
        style_name = (
            name_node.attrib.get(f"{{{_WORD_NS}}}val", "") if name_node is not None else ""
        )
        level = _docx_heading_level(style_id) or _docx_heading_level(style_name)
        if level is not None:
            result[style_id] = level
    return result


def _docx_paragraph_style(paragraph: ElementTree.Element) -> str:
    properties = paragraph.find(f"{{{_WORD_NS}}}pPr")
    if properties is None:
        return ""
    style = properties.find(f"{{{_WORD_NS}}}pStyle")
    if style is None:
        return ""
    return style.attrib.get(f"{{{_WORD_NS}}}val", "")


def _docx_has_section_break(paragraph: ElementTree.Element) -> bool:
    properties = paragraph.find(f"{{{_WORD_NS}}}pPr")
    return properties is not None and properties.find(f"{{{_WORD_NS}}}sectPr") is not None


def _docx_preview(
    path: Path, *, max_chars: int
) -> tuple[str, bool, list[DocumentHeading], int]:
    with zipfile.ZipFile(path) as zf:
        _validate_zip(zf)
        raw = _read_zip_member(zf, "word/document.xml")
        heading_styles = _docx_heading_styles(zf)
        assert raw is not None
    root = _parse_xml(raw)
    body = root.find(f"{{{_WORD_NS}}}body")
    if body is None:
        raise ValueError("DOCX document body is missing")

    section_breaks = sum(
        1
        for child in body
        if child.tag == f"{{{_WORD_NS}}}p" and _docx_has_section_break(child)
    )
    parts: list[str] = []
    headings: list[DocumentHeading] = []
    truncated = False
    for child in body:
        if child.tag == f"{{{_WORD_NS}}}p":
            text = "".join(node.text or "" for node in child.iter(f"{{{_WORD_NS}}}t"))
            style_id = _docx_paragraph_style(child)
            heading_level = heading_styles.get(style_id) or _docx_heading_level(style_id)
            if text:
                rendered = text + "\n"
                if heading_level is not None:
                    headings.append(DocumentHeading(level=heading_level, text=text[:1024]))
                    rendered = f"{'#' * heading_level} {text}\n"
                if _append_bounded(parts, rendered, max_chars=max_chars):
                    truncated = True
                    break
            if _docx_has_section_break(child):
                if _append_bounded(parts, "\n---\n", max_chars=max_chars):
                    truncated = True
                    break
        elif child.tag == f"{{{_WORD_NS}}}tbl":
            for row in child.findall(f"{{{_WORD_NS}}}tr"):
                cells = [
                    "".join(node.text or "" for node in cell.iter(f"{{{_WORD_NS}}}t"))
                    for cell in row.findall(f"{{{_WORD_NS}}}tc")
                ]
                if _append_bounded(parts, "\t".join(cells) + "\n", max_chars=max_chars):
                    truncated = True
                    break
            if truncated:
                break
    return "".join(parts), truncated, headings, section_breaks


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    raw = _read_zip_member(zf, "xl/sharedStrings.xml", required=False)
    if raw is None:
        return []
    root = _parse_xml(raw)
    return [
        "".join(node.text or "" for node in item.iter(f"{{{_SHEET_NS}}}t"))
        for item in root.findall(f"{{{_SHEET_NS}}}si")
    ]


def _xlsx_sheet_map(zf: zipfile.ZipFile) -> tuple[list[str], dict[str, str]]:
    workbook_raw = _read_zip_member(zf, "xl/workbook.xml")
    rels_raw = _read_zip_member(zf, "xl/_rels/workbook.xml.rels")
    assert workbook_raw is not None and rels_raw is not None
    workbook = _parse_xml(workbook_raw)
    rels = _parse_xml(rels_raw)
    rel_targets = {
        rel.attrib.get("Id", ""): rel.attrib.get("Target", "")
        for rel in rels.findall(f"{{{_PKG_REL_NS}}}Relationship")
    }
    names: list[str] = []
    targets: dict[str, str] = {}
    sheets = workbook.find(f"{{{_SHEET_NS}}}sheets")
    if sheets is None:
        return names, targets
    for node in sheets.findall(f"{{{_SHEET_NS}}}sheet"):
        name = node.attrib.get("name", "")
        rel_id = node.attrib.get(f"{{{_REL_NS}}}id", "")
        target = rel_targets.get(rel_id, "")
        if not name or not target:
            continue
        pure = PurePosixPath(target.lstrip("/"))
        if pure.is_absolute() or ".." in pure.parts:
            raise ValueError("XLSX worksheet relationship escapes the workbook")
        member = (
            str(pure) if pure.parts and pure.parts[0] == "xl" else str(PurePosixPath("xl") / pure)
        )
        if not member.startswith("xl/worksheets/"):
            raise ValueError("XLSX worksheet relationship has an unexpected target")
        names.append(name)
        targets[name] = member
    return names, targets


def _column_index(reference: str) -> int | None:
    match = re.match(r"^([A-Z]+)", reference.upper())
    if match is None:
        return None
    value = 0
    for char in match.group(1):
        value = value * 26 + (ord(char) - ord("A") + 1)
    return value - 1


def _column_letters(index: int) -> str:
    value = index + 1
    chars: list[str] = []
    while value:
        value, remainder = divmod(value - 1, 26)
        chars.append(chr(ord("A") + remainder))
    return "".join(reversed(chars))


def _parse_cell_reference(reference: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"([A-Za-z]+)([1-9][0-9]*)", reference)
    if match is None:
        return None
    column = _column_index(match.group(1))
    if column is None:
        return None
    return column, int(match.group(2))


def _parse_cell_range(cell_range: str) -> tuple[int, int, int, int, str]:
    parts = cell_range.split(":", 1)
    if len(parts) != 2:
        raise ValueError("XLSX cell_range must use A1:D100 syntax")
    start = _parse_cell_reference(parts[0])
    end = _parse_cell_reference(parts[1])
    if start is None or end is None:
        raise ValueError("XLSX cell_range must use A1:D100 syntax")
    start_col, start_row = start
    end_col, end_row = end
    if start_col > end_col or start_row > end_row:
        raise ValueError("XLSX cell_range start must not be after its end")
    if end_col >= XLSX_MAX_COLUMNS:
        raise ValueError(f"XLSX cell_range exceeds {XLSX_MAX_COLUMNS} column limit")
    if end_row > XLSX_MAX_ROWS:
        raise ValueError("XLSX cell_range exceeds worksheet row limit")
    normalized = (
        f"{_column_letters(start_col)}{start_row}:"
        f"{_column_letters(end_col)}{end_row}"
    )
    return start_col, start_row, end_col, end_row, normalized


def _xlsx_cell_text(cell: ElementTree.Element, shared: list[str]) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{{{_SHEET_NS}}}t"))
    value = cell.find(f"{{{_SHEET_NS}}}v")
    raw = value.text if value is not None and value.text is not None else ""
    if cell_type == "s" and raw:
        try:
            index = int(raw)
            return shared[index] if 0 <= index < len(shared) else raw
        except ValueError:
            return raw
    if cell_type == "b":
        return "TRUE" if raw == "1" else "FALSE" if raw == "0" else raw
    return raw


def _xlsx_preview(
    path: Path,
    *,
    sheet: str | None,
    cell_range: str | None,
    max_rows: int,
    max_chars: int,
) -> tuple[str, list[str], str | None, str | None, int, bool]:
    bounds = _parse_cell_range(cell_range) if cell_range else None
    with zipfile.ZipFile(path) as zf:
        _validate_zip(zf)
        sheets, targets = _xlsx_sheet_map(zf)
        if not sheets:
            raise ValueError("XLSX workbook has no worksheets")
        selected = sheet or sheets[0]
        if selected not in targets:
            raise ValueError(f"XLSX worksheet not found: {selected}")
        shared = _xlsx_shared_strings(zf)
        raw = _read_zip_member(zf, targets[selected])
        assert raw is not None
    root = _parse_xml(raw)
    sheet_data = root.find(f"{{{_SHEET_NS}}}sheetData")
    if sheet_data is None:
        normalized = bounds[4] if bounds else None
        return "", sheets, selected, normalized, 0, False

    if bounds is not None:
        start_col, start_row, end_col, end_row, normalized = bounds
        total_requested_rows = end_row - start_row + 1
        row_limit = min(total_requested_rows, max_rows)
        effective_end_row = start_row + row_limit - 1
        selected_rows: dict[int, dict[int, str]] = {}
        fallback_row = 0
        for row in sheet_data.findall(f"{{{_SHEET_NS}}}row"):
            fallback_row += 1
            raw_row = row.attrib.get("r", "")
            try:
                row_number = int(raw_row) if raw_row else fallback_row
            except ValueError:
                row_number = fallback_row
            if row_number < start_row or row_number > effective_end_row:
                continue
            cells: dict[int, str] = {}
            for cell in row.findall(f"{{{_SHEET_NS}}}c"):
                parsed = _parse_cell_reference(cell.attrib.get("r", ""))
                index = parsed[0] if parsed is not None else _column_index(cell.attrib.get("r", ""))
                if index is None or index < start_col or index > end_col:
                    continue
                cells[index] = _xlsx_cell_text(cell, shared)
            selected_rows[row_number] = cells

        parts: list[str] = []
        rows_returned = 0
        truncated = total_requested_rows > max_rows
        for row_number in range(start_row, effective_end_row + 1):
            cells = selected_rows.get(row_number, {})
            line = "\t".join(
                cells.get(index, "") for index in range(start_col, end_col + 1)
            ) + "\n"
            if _append_bounded(parts, line, max_chars=max_chars):
                truncated = True
                break
            rows_returned += 1
        return "".join(parts), sheets, selected, normalized, rows_returned, truncated

    parts: list[str] = []
    rows_returned = 0
    truncated = False
    for row in sheet_data.findall(f"{{{_SHEET_NS}}}row"):
        if rows_returned >= max_rows:
            truncated = True
            break
        cells: dict[int, str] = {}
        overflow_column = False
        for cell in row.findall(f"{{{_SHEET_NS}}}c"):
            index = _column_index(cell.attrib.get("r", ""))
            if index is None:
                index = len(cells)
            if index >= XLSX_MAX_COLUMNS:
                overflow_column = True
                continue
            cells[index] = _xlsx_cell_text(cell, shared)
        last = max(cells, default=-1)
        line = "\t".join(cells.get(index, "") for index in range(last + 1)) + "\n"
        if _append_bounded(parts, line, max_chars=max_chars):
            truncated = True
            break
        rows_returned += 1
        if overflow_column:
            truncated = True
    return "".join(parts), sheets, selected, None, rows_returned, truncated


def _pdf_page_count(path: Path) -> int:
    if not PDFINFO_PATH.is_file() or not os.access(PDFINFO_PATH, os.X_OK):
        raise ValueError("PDF metadata extraction is unavailable on this Agent")
    process = subprocess.run(
        [str(PDFINFO_PATH), str(path)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if process.returncode != 0:
        error = process.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(error[:512] or "PDF metadata extraction failed")
    output = process.stdout.decode("utf-8", errors="replace")
    match = re.search(r"^Pages:\s+(\d+)\s*$", output, flags=re.IGNORECASE | re.MULTILINE)
    if match is None or int(match.group(1)) < 1:
        raise ValueError("PDF page count is unavailable")
    return int(match.group(1))


def _pdf_preview(
    path: Path, *, page: int, max_pages: int, max_chars: int
) -> tuple[str, bool, int, int, int | None]:
    if not PDFTOTEXT_PATH.is_file() or not os.access(PDFTOTEXT_PATH, os.X_OK):
        raise ValueError("PDF text extraction is unavailable on this Agent")
    pages_total = _pdf_page_count(path)
    if page > pages_total:
        raise ValueError(f"PDF page {page} exceeds total pages {pages_total}")
    pages_returned = min(max_pages, pages_total - page + 1)
    last_page = page + pages_returned - 1
    process = subprocess.run(
        [
            str(PDFTOTEXT_PATH),
            "-f",
            str(page),
            "-l",
            str(last_page),
            "-layout",
            "-nopgbrk",
            str(path),
            "-",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if process.returncode != 0:
        error = process.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(error[:512] or "PDF text extraction failed")
    text = process.stdout.decode("utf-8", errors="replace")
    truncated = len(text) > max_chars
    next_page = last_page + 1 if last_page < pages_total else None
    return text[:max_chars], truncated, pages_total, pages_returned, next_page


def _preview_document_sync(
    request_id: str,
    raw_path: str,
    *,
    roots: list[Path],
    page: int,
    max_pages: int,
    sheet: str | None,
    cell_range: str | None,
    max_rows: int,
    max_chars: int,
) -> DocumentPreviewResult:
    try:
        requested = Path(raw_path).expanduser()
        if not requested.is_absolute():
            raise PermissionError("document path must be absolute")
        if requested.is_symlink():
            raise PermissionError("symlink documents are not supported")
        path = resolve_allowed_path(raw_path, roots).resolve(strict=True)
        if not path.is_file():
            raise PermissionError("not a regular file")
        size = path.stat().st_size
        if size > DOCUMENT_MAX_INPUT_BYTES:
            raise ValueError("document exceeds size limit")
        sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        suffix = path.suffix.lower()
        if suffix == ".docx":
            content, truncated, headings, section_breaks = _docx_preview(
                path, max_chars=max_chars
            )
            return DocumentPreviewResult(
                request_id=request_id,
                path=str(path),
                kind="docx",
                content=content,
                size=size,
                sha256=sha256,
                headings=headings,
                section_breaks=section_breaks,
                truncated=truncated,
            )
        if suffix == ".xlsx":
            content, sheets, selected, normalized_range, rows_returned, truncated = _xlsx_preview(
                path,
                sheet=sheet,
                cell_range=cell_range,
                max_rows=max_rows,
                max_chars=max_chars,
            )
            return DocumentPreviewResult(
                request_id=request_id,
                path=str(path),
                kind="xlsx",
                content=content,
                size=size,
                sha256=sha256,
                sheets=sheets,
                sheet=selected,
                cell_range=normalized_range,
                rows_returned=rows_returned,
                truncated=truncated,
            )
        if suffix == ".pdf":
            content, truncated, pages_total, pages_returned, next_page = _pdf_preview(
                path, page=page, max_pages=max_pages, max_chars=max_chars
            )
            return DocumentPreviewResult(
                request_id=request_id,
                path=str(path),
                kind="pdf",
                content=content,
                size=size,
                sha256=sha256,
                page=page,
                pages_requested=max_pages,
                pages_total=pages_total,
                pages_returned=pages_returned,
                next_page=next_page,
                truncated=truncated,
            )
        raise ValueError("unsupported document type; expected .pdf, .docx, or .xlsx")
    except (
        OSError,
        PermissionError,
        subprocess.TimeoutExpired,
        ValueError,
        zipfile.BadZipFile,
        ElementTree.ParseError,
    ) as exc:
        return DocumentPreviewResult(request_id=request_id, rejected=True, error=str(exc))


async def preview_document(
    request_id: str,
    raw_path: str,
    *,
    roots: list[Path],
    page: int = 1,
    max_pages: int = 5,
    sheet: str | None = None,
    cell_range: str | None = None,
    max_rows: int = 200,
    max_chars: int = 65_536,
) -> DocumentPreviewResult:
    return await asyncio.to_thread(
        _preview_document_sync,
        request_id,
        raw_path,
        roots=roots,
        page=page,
        max_pages=max_pages,
        sheet=sheet,
        cell_range=cell_range,
        max_rows=max_rows,
        max_chars=max_chars,
    )

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
from remote_mcp_commander.protocol import DocumentPreviewResult

PDFTOTEXT_PATH = Path("/usr/bin/pdftotext")
DOCUMENT_MAX_INPUT_BYTES = 33_554_432
DOCUMENT_MAX_ZIP_ENTRIES = 4096
DOCUMENT_MAX_UNCOMPRESSED_BYTES = 67_108_864
DOCUMENT_MAX_XML_BYTES = 16_777_216
XLSX_MAX_COLUMNS = 256

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


def _docx_preview(path: Path, *, max_chars: int) -> tuple[str, bool]:
    with zipfile.ZipFile(path) as zf:
        _validate_zip(zf)
        raw = _read_zip_member(zf, "word/document.xml")
        assert raw is not None
    root = _parse_xml(raw)
    body = root.find(f"{{{_WORD_NS}}}body")
    if body is None:
        raise ValueError("DOCX document body is missing")
    parts: list[str] = []
    truncated = False
    for child in body:
        if child.tag == f"{{{_WORD_NS}}}p":
            text = "".join(node.text or "" for node in child.iter(f"{{{_WORD_NS}}}t"))
            if text and _append_bounded(parts, text + "\n", max_chars=max_chars):
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
    return "".join(parts), truncated


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
    match = re.match(r"^([A-Z]+)", reference)
    if match is None:
        return None
    value = 0
    for char in match.group(1):
        value = value * 26 + (ord(char) - ord("A") + 1)
    return value - 1


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
    path: Path, *, sheet: str | None, max_rows: int, max_chars: int
) -> tuple[str, list[str], str | None, int, bool]:
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
        return "", sheets, selected, 0, False
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
    return "".join(parts), sheets, selected, rows_returned, truncated


def _pdf_preview(path: Path, *, page: int, max_pages: int, max_chars: int) -> tuple[str, bool]:
    if not PDFTOTEXT_PATH.is_file() or not os.access(PDFTOTEXT_PATH, os.X_OK):
        raise ValueError("PDF text extraction is unavailable on this Agent")
    process = subprocess.run(
        [
            str(PDFTOTEXT_PATH),
            "-f",
            str(page),
            "-l",
            str(page + max_pages - 1),
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
    return (text[:max_chars], True) if len(text) > max_chars else (text, False)


def _preview_document_sync(
    request_id: str,
    raw_path: str,
    *,
    roots: list[Path],
    page: int,
    max_pages: int,
    sheet: str | None,
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
            content, truncated = _docx_preview(path, max_chars=max_chars)
            return DocumentPreviewResult(
                request_id=request_id,
                path=str(path),
                kind="docx",
                content=content,
                size=size,
                sha256=sha256,
                truncated=truncated,
            )
        if suffix == ".xlsx":
            content, sheets, selected, rows_returned, truncated = _xlsx_preview(
                path, sheet=sheet, max_rows=max_rows, max_chars=max_chars
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
                rows_returned=rows_returned,
                truncated=truncated,
            )
        if suffix == ".pdf":
            content, truncated = _pdf_preview(
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
        max_rows=max_rows,
        max_chars=max_chars,
    )

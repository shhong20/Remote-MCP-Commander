from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape

from remote_mcp_commander.agent.document_ops import DOCUMENT_MAX_INPUT_BYTES
from remote_mcp_commander.agent.file_ops import file_sha256
from remote_mcp_commander.agent.pdf_ops import (
    _bounded_error,
    _fsync_directory,
    _pdf_page_count,
    _reject_unsafe_pdf_features,
    _remaining_timeout,
    _require_tool,
    _resolve_output,
    _revalidate_output,
)
from remote_mcp_commander.protocol import PdfRenderResult

SOFFICE_PATH = Path("/usr/bin/soffice")
PDF_RENDER_MAX_MARKDOWN_BYTES = 262_144
PDF_RENDER_MAX_PAGES = 200
PDF_RENDER_TIMEOUT_S = 90.0
_PAGE_BREAK_MARKERS = {
    '<div style="page-break-before: always;"></div>',
    "<!-- pagebreak -->",
}
_INLINE_TOKEN = re.compile(r"(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*)")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_UNORDERED = re.compile(r"^\s*[-+*]\s+(.+?)\s*$")
_ORDERED = re.compile(r"^\s*(\d+)[.)]\s+(.+?)\s*$")
_HORIZONTAL_RULE = re.compile(r"^\s*(?:---|\*\*\*|___)\s*$")


def _xml_safe(value: str) -> str:
    def valid(char: str) -> bool:
        code = ord(char)
        return (
            code in {0x09, 0x0A, 0x0D}
            or 0x20 <= code <= 0xD7FF
            or 0xE000 <= code <= 0xFFFD
            or 0x10000 <= code <= 0x10FFFF
        )

    return "".join(char if valid(char) else "\uFFFD" for char in value)


def _inline_markup(value: str) -> str:
    value = _xml_safe(value)
    parts: list[str] = []
    cursor = 0
    for match in _INLINE_TOKEN.finditer(value):
        parts.append(escape(value[cursor : match.start()]))
        token = match.group(0)
        if token.startswith("`"):
            parts.append(
                f'<text:span text:style-name="InlineCode">{escape(token[1:-1])}</text:span>'
            )
        elif token.startswith("**"):
            parts.append(
                f'<text:span text:style-name="Bold">{escape(token[2:-2])}</text:span>'
            )
        else:
            parts.append(
                f'<text:span text:style-name="Italic">{escape(token[1:-1])}</text:span>'
            )
        cursor = match.end()
    parts.append(escape(value[cursor:]))
    return "".join(parts)


def _paragraph(style: str, text: str, *, preserve: bool = False) -> str:
    preserve_attr = ' xml:space="preserve"' if preserve else ""
    body = escape(_xml_safe(text)) if preserve else _inline_markup(text)
    return f'<text:p text:style-name="{style}"{preserve_attr}>{body}</text:p>'


def _markdown_blocks(markdown: str) -> list[str]:
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[str] = []
    paragraph_lines: list[str] = []
    code_lines: list[str] = []
    in_code = False

    def flush_paragraph() -> None:
        if paragraph_lines:
            text = " ".join(item.strip() for item in paragraph_lines if item.strip())
            if text:
                blocks.append(_paragraph("Body", text))
            paragraph_lines.clear()

    def flush_code() -> None:
        nonlocal code_lines
        if code_lines:
            for line in code_lines:
                blocks.append(_paragraph("Code", line, preserve=True))
        else:
            blocks.append(_paragraph("Code", "", preserve=True))
        code_lines = []

    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            flush_paragraph()
            if in_code:
                flush_code()
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_lines.append(raw_line)
            continue
        if stripped in _PAGE_BREAK_MARKERS:
            flush_paragraph()
            blocks.append('<text:p text:style-name="PageBreak"/>')
            continue
        if not stripped:
            flush_paragraph()
            continue
        heading = _HEADING.match(raw_line)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            blocks.append(_paragraph(f"H{level}", heading.group(2)))
            continue
        if _HORIZONTAL_RULE.match(raw_line):
            flush_paragraph()
            blocks.append(_paragraph("Rule", "────────────────────────────────────────"))
            continue
        unordered = _UNORDERED.match(raw_line)
        if unordered:
            flush_paragraph()
            blocks.append(_paragraph("List", f"• {unordered.group(1)}"))
            continue
        ordered = _ORDERED.match(raw_line)
        if ordered:
            flush_paragraph()
            blocks.append(_paragraph("List", f"{ordered.group(1)}. {ordered.group(2)}"))
            continue
        if stripped.startswith(">"):
            flush_paragraph()
            blocks.append(_paragraph("Quote", stripped[1:].lstrip()))
            continue
        paragraph_lines.append(raw_line)

    if in_code:
        raise ValueError("Markdown fenced code block is not closed")
    flush_paragraph()
    return blocks


def _build_fodt(markdown: str) -> str:
    blocks = _markdown_blocks(markdown)
    if not blocks:
        raise ValueError("Markdown content is empty")
    styles: list[str] = []
    heading_sizes = {1: 24, 2: 20, 3: 17, 4: 14, 5: 12, 6: 11}
    for level, size in heading_sizes.items():
        styles.append(
            f'<style:style style:name="H{level}" style:family="paragraph">'
            '<style:paragraph-properties '
            'fo:margin-top="0.28cm" fo:margin-bottom="0.18cm"/>'
            '<style:text-properties style:font-name="NanumGothic" '
            f'fo:font-size="{size}pt" fo:font-weight="bold"/>'
            '</style:style>'
        )
    styles.extend(
        [
            (
                '<style:style style:name="Body" style:family="paragraph">'
                '<style:paragraph-properties fo:margin-bottom="0.16cm" '
                'fo:line-height="135%"/>'
                '<style:text-properties style:font-name="NanumGothic" '
                'fo:font-size="10.5pt"/></style:style>'
            ),
            (
                '<style:style style:name="List" style:family="paragraph">'
                '<style:paragraph-properties fo:margin-left="0.55cm" '
                'fo:margin-bottom="0.10cm"/>'
                '<style:text-properties style:font-name="NanumGothic" '
                'fo:font-size="10.5pt"/></style:style>'
            ),
            (
                '<style:style style:name="Quote" style:family="paragraph">'
                '<style:paragraph-properties fo:margin-left="0.55cm" '
                'fo:margin-right="0.3cm" fo:padding-left="0.18cm" '
                'fo:border-left="0.04cm solid #9CA3AF"/>'
                '<style:text-properties style:font-name="NanumGothic" '
                'fo:font-size="10pt" fo:font-style="italic" '
                'fo:color="#4B5563"/></style:style>'
            ),
            (
                '<style:style style:name="Code" style:family="paragraph">'
                '<style:paragraph-properties fo:margin-left="0.25cm" '
                'fo:margin-right="0.25cm" fo:margin-bottom="0cm" '
                'fo:padding="0.08cm" fo:background-color="#F3F4F6"/>'
                '<style:text-properties style:font-name="NanumGothicCoding" '
                'fo:font-size="9pt"/></style:style>'
            ),
            (
                '<style:style style:name="Rule" style:family="paragraph">'
                '<style:paragraph-properties fo:margin-top="0.15cm" '
                'fo:margin-bottom="0.2cm"/>'
                '<style:text-properties style:font-name="NanumGothic" '
                'fo:font-size="9pt" fo:color="#9CA3AF"/></style:style>'
            ),
            (
                '<style:style style:name="PageBreak" style:family="paragraph">'
                '<style:paragraph-properties fo:break-before="page"/>'
                '</style:style>'
            ),
            (
                '<style:style style:name="Bold" style:family="text">'
                '<style:text-properties fo:font-weight="bold"/>'
                '</style:style>'
            ),
            (
                '<style:style style:name="Italic" style:family="text">'
                '<style:text-properties fo:font-style="italic"/>'
                '</style:style>'
            ),
            (
                '<style:style style:name="InlineCode" style:family="text">'
                '<style:text-properties style:font-name="NanumGothicCoding" '
                'fo:background-color="#F3F4F6"/></style:style>'
            ),
        ]
    )
    page_layout = (
        '<style:page-layout style:name="A4">'
        '<style:page-layout-properties fo:page-width="21cm" '
        'fo:page-height="29.7cm" style:print-orientation="portrait" '
        'fo:margin-top="1.8cm" fo:margin-bottom="1.8cm" '
        'fo:margin-left="1.8cm" fo:margin-right="1.8cm"/>'
        '</style:page-layout>'
    )
    master = (
        '<office:master-styles>'
        '<style:master-page style:name="Standard" style:page-layout-name="A4"/>'
        '</office:master-styles>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
        'office:mimetype="application/vnd.oasis.opendocument.text" '
        'office:version="1.3">'
        '<office:font-face-decls>'
        '<style:font-face style:name="NanumGothic" '
        'style:font-family-generic="swiss"/>'
        '<style:font-face style:name="NanumGothicCoding" '
        'style:font-family-generic="modern"/>'
        '</office:font-face-decls>'
        f'<office:styles>{"".join(styles)}</office:styles>'
        f'<office:automatic-styles>{page_layout}</office:automatic-styles>'
        f'{master}'
        f'<office:body><office:text>{"".join(blocks)}</office:text></office:body>'
        '</office:document>'
    )


def _render_pdf_sync(
    request_id: str,
    output_path: str,
    markdown: str,
    *,
    roots: list[Path],
    overwrite: bool,
    expected_sha256: str | None,
) -> PdfRenderResult:
    temp_dir: Path | None = None
    deadline = time.monotonic() + PDF_RENDER_TIMEOUT_S
    try:
        _require_tool(SOFFICE_PATH, "LibreOffice Writer")
        if len(markdown.encode("utf-8")) > PDF_RENDER_MAX_MARKDOWN_BYTES:
            raise ValueError("Markdown input exceeds UTF-8 byte limit")
        if not markdown.strip():
            raise ValueError("Markdown content is empty")
        output, existed, mode, output_identity = _resolve_output(
            output_path,
            roots,
            overwrite=overwrite,
            expected_sha256=expected_sha256,
        )
        temp_dir = Path(tempfile.mkdtemp(prefix=".remote-mcp-render-", dir=output.parent))
        if os.name != "nt":
            os.chmod(temp_dir, 0o700)
        fodt_path = temp_dir / "document.fodt"
        fodt_path.write_text(_build_fodt(markdown), encoding="utf-8")
        if os.name != "nt":
            os.chmod(fodt_path, 0o600)
        profile_dir = temp_dir / "profile"
        profile_dir.mkdir(mode=0o700)
        process = subprocess.run(
            [
                str(SOFFICE_PATH),
                "--safe-mode",
                "--headless",
                "--nologo",
                "--nodefault",
                "--nofirststartwizard",
                "--nolockcheck",
                f"-env:UserInstallation={profile_dir.as_uri()}",
                "--convert-to",
                "pdf:writer_pdf_Export",
                "--outdir",
                str(temp_dir),
                str(fodt_path),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_remaining_timeout(deadline, 75.0),
            check=False,
        )
        if process.returncode != 0:
            raise ValueError(_bounded_error(process, "PDF rendering failed"))
        temp_output = temp_dir / "document.pdf"
        if not temp_output.is_file():
            raise ValueError("PDF rendering did not produce an output file")
        if temp_output.stat().st_size > DOCUMENT_MAX_INPUT_BYTES:
            raise ValueError("rendered PDF exceeds size limit")
        pages = _pdf_page_count(
            temp_output, timeout_s=_remaining_timeout(deadline, 15.0)
        )
        if pages > PDF_RENDER_MAX_PAGES:
            raise ValueError(f"rendered PDF exceeds {PDF_RENDER_MAX_PAGES} page limit")
        _reject_unsafe_pdf_features(temp_output, deadline=deadline)
        if os.name != "nt":
            os.chmod(temp_output, mode)
        with temp_output.open("rb") as handle:
            os.fsync(handle.fileno())
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
                raise ValueError("PDF output target appeared during rendering") from exc
            temp_output.unlink()
        _fsync_directory(output.parent)
        return PdfRenderResult(
            request_id=request_id,
            output_path=str(output),
            pages_written=pages,
            bytes_written=output.stat().st_size,
            sha256=file_sha256(output),
        )
    except (
        OSError,
        PermissionError,
        subprocess.TimeoutExpired,
        ValueError,
    ) as exc:
        return PdfRenderResult(
            request_id=request_id,
            output_path=output_path,
            rejected=True,
            error=str(exc),
        )
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


async def render_pdf_from_markdown(
    request_id: str,
    output_path: str,
    markdown: str,
    *,
    roots: list[Path],
    overwrite: bool = False,
    expected_sha256: str | None = None,
) -> PdfRenderResult:
    return await asyncio.to_thread(
        _render_pdf_sync,
        request_id,
        output_path,
        markdown,
        roots=roots,
        overwrite=overwrite,
        expected_sha256=expected_sha256,
    )

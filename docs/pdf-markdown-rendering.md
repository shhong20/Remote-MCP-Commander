# Markdown PDF rendering

`create_pdf_from_markdown` creates a new bounded PDF from a safe Markdown subset on one connected Agent.

## Safety model

- output must be an absolute `.pdf` path inside an Agent allowed root
- symlink outputs are rejected
- new files are published with owner-only mode by default
- overwriting requires the current `expected_sha256`
- rendering happens in a private `0700` temporary directory
- the generated Flat ODT source is `0600`
- LibreOffice Writer runs headless in `--safe-mode` with a unique temporary profile
- generated PDFs are re-inspected for JavaScript and embedded attachments before publication
- publication is atomic and guarded against target races
- the Markdown body and overwrite SHA are not written to Gateway audit records

## Bounds

- Markdown input: 262,144 UTF-8 bytes maximum
- output PDF: 32 MiB maximum
- rendered pages: 200 maximum
- render deadline: 90 seconds
- Gateway request window: at least 105 seconds

## Supported Markdown subset

- `#` through `######` headings
- paragraphs
- unordered lists using `-`, `+`, or `*`
- ordered lists such as `1.` or `1)`
- blockquotes beginning with `>`
- fenced code blocks using triple backticks
- inline `code`, `**bold**`, and `*italic*`
- horizontal rules using `---`, `***`, or `___`
- the exact marker `<div style="page-break-before: always;"></div>` or `<!-- pagebreak -->` for a page break

All other raw HTML is escaped and rendered as text. Markdown images, external resources, scripts, user CSS, and network fetching are intentionally not supported.

## Fonts

The renderer uses `NanumGothic` for normal text and `NanumGothicCoding` for code. The deployment/CI environment should provide `fonts-nanum`.

## Runtime dependency

The Agent requires `/usr/bin/soffice` from LibreOffice Writer. `pdf.render` is advertised only when that executable is available. CI installs `libreoffice-writer` and exercises the real conversion path.

## Overwrite flow

1. read the existing output SHA-256
2. call `create_pdf_from_markdown(..., overwrite=True, expected_sha256=<current sha>)`
3. rendering completes to a private temporary PDF
4. the output target identity and SHA are checked again
5. the new PDF is atomically published

A stale hash or a target that appears/changes during rendering fails closed.

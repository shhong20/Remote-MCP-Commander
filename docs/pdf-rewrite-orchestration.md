# PDF rewrite orchestration

`rewrite_pdf_with_markdown` creates a new PDF from one existing PDF by declaring the final page sequence.

## Model

The input PDF is never edited in place. The caller supplies:

- `source_path`
- `output_path`
- the exact `expected_source_sha256`
- an ordered `segments` list

Two segment kinds are supported:

- `source_pages`: select a bounded page range from the original PDF
- `markdown`: render a bounded safe Markdown fragment as one or more new PDF pages

Deleting pages means omitting them from the final sequence. Reordering means placing source page ranges in a different order. Inserting content means placing a Markdown segment between source page segments.

Example final sequence:

1. source pages 1-2
2. Markdown insertion
3. source page 5
4. source pages 3-4

## Integrity and publication

- the source must be an absolute regular `.pdf` inside an Agent allowed root
- source symlinks are rejected
- source PDF JavaScript and embedded attachments are rejected
- the caller must provide the current source SHA-256
- source identity and SHA are revalidated throughout the operation and again before publication
- `output_path` must differ from `source_path`
- new outputs are published atomically and default to owner-only permissions
- overwriting an existing output requires its current `expected_output_sha256`
- the output target identity/SHA is revalidated immediately before publication

## Markdown isolation

Markdown segments reuse the safe local renderer:

- generated Flat ODT only
- LibreOffice Writer `--safe-mode`
- private per-segment profile inside a private rewrite temp directory
- no network fetch
- no raw HTML execution
- no scripts, external images, user CSS, or macros

Intermediate rendered PDFs are never published into the allowed root. They remain inside the private temporary rewrite directory until final composition finishes.

## Bounds

- maximum segments: 32
- aggregate Markdown input: 262,144 UTF-8 bytes
- maximum final pages: 200
- maximum tracked intermediate PDF data: 96 MiB
- final PDF: 32 MiB
- Agent deadline: 120 seconds

## Audit

The Gateway audit records only operational metadata such as output path, segment counts, page count, byte count, overwrite flag, and rejected state.

The audit does not include:

- source PDF path
- source or output SHA guards
- Markdown contents
- extracted page contents
- generated PDF bytes

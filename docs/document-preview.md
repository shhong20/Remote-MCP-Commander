# Document preview

`preview_document` provides bounded, read-only text previews for documents already inside an Agent allowed root. Existing v0.59 calls remain valid; v0.60 adds navigation and structure metadata without changing the required arguments.

## Supported formats

- PDF: extracts text from a bounded page range with fixed `/usr/bin/pdfinfo` and `/usr/bin/pdftotext` binaries. Results include `pages_total`, `pages_returned`, and `next_page`, so callers can walk a document without guessing page bounds.
- DOCX: reads Word XML directly, renders recognized Heading 1..9 paragraphs as Markdown headings, returns structured `headings`, preserves paragraph section breaks as `---`, and reports `section_breaks`.
- XLSX: reads workbook XML directly, lists worksheet names, and returns one selected sheet as tab-separated rows. Optional `cell_range` accepts an A1-style rectangle such as `B2:F40`; the normalized range is returned in the result.

## Navigation examples

For a PDF, request `page=1, max_pages=5`, then use `next_page` for the next call until it becomes `null`. A request beyond `pages_total` fails closed instead of silently returning an empty preview.

For XLSX, `sheet="Data", cell_range="B2:F40"` returns only that rectangle. Range output preserves blank rows and columns inside the rectangle. `max_rows` and `max_chars` still cap the returned payload even when a larger range is requested.

DOCX previews keep ordinary paragraphs and table rows while recognized headings are emitted as `#`, `##`, and so on. The `headings` array contains the same heading level/text pairs for structured callers.

## Safety boundaries

The tool never executes macros, follows external document links, or writes document content. Final-path symlinks are rejected. Input files are limited to 32 MiB. ZIP-based documents are limited by entry count and total uncompressed size, reject encrypted or duplicate entries, and reject XML DTD/entity declarations. Individual XML members are bounded to 16 MiB.

PDF extraction accepts at most 20 pages per call and the returned text is bounded by `max_chars`. XLSX output is limited by `max_rows`, `max_chars`, and 256 columns; A1 ranges must remain within that 256-column preview boundary and the worksheet row limit. Unsupported file types and invalid/reversed ranges fail closed.

This preview is intentionally text-oriented. Images, embedded objects, styling other than heading levels, formulas beyond cached cell values, and document editing remain outside this interface.

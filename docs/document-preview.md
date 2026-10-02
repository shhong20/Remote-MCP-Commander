# Document preview

`preview_document` provides bounded, read-only text previews for documents already inside an Agent allowed root.

Supported formats:

- PDF: extracts text from a bounded page range with the fixed `/usr/bin/pdftotext` binary.
- DOCX: reads `word/document.xml` directly and returns paragraphs plus table rows.
- XLSX: reads workbook XML directly, lists worksheet names, and returns one selected sheet as tab-separated rows.

## Safety boundaries

The tool never executes macros, follows external document links, or writes document content. Final-path symlinks are rejected. Input files are limited to 32 MiB. ZIP-based documents are limited by entry count and total uncompressed size, reject encrypted or duplicate entries, and reject XML DTD/entity declarations. Individual XML members are bounded to 16 MiB.

PDF extraction accepts at most 20 pages per call and the returned text is bounded by `max_chars`. XLSX output is limited by `max_rows`, `max_chars`, and 256 columns. Unsupported file types fail closed.

This preview is intentionally text-oriented. Images, embedded objects, styling, formulas beyond cached cell values, and document editing remain outside this interface.

# Structured document editing

Remote MCP Commander v0.68 adds bounded, SHA-guarded edits for `.docx` and `.xlsx` files inside Agent allowed roots.

## Tools

### `replace_docx_text`

- edits WordprocessingML text in the document body and header/footer XML members
- exact paragraph-level matching only
- supports matches split across multiple `<w:t>` runs without flattening the entire paragraph
- does not match across tabs, line breaks, drawings, field codes, notes, symbols, or similar structural separators
- `expected_replacements` must exactly match the number found before any write occurs
- rejects macro/ActiveX-bearing archives
- control characters are not accepted as replacement text

### `edit_xlsx_range`

- edits one explicit worksheet and A1 rectangular range
- the 2D values array must exactly match the range dimensions
- existing cell styles are retained when values are assigned
- formulas outside the edited range are preserved
- formula injection through values beginning with `=` is rejected
- merged-cell intersections are rejected
- macro/ActiveX-bearing archives are rejected
- input is limited to 4,096 cells and 512 KiB of UTF-8 string payload before the Gateway-to-Agent WebSocket dispatch

## Integrity and publication

Both tools require the SHA-256 returned by a previous document preview. The Agent verifies the hash before editing and revalidates file identity, timestamps, size, mode, and SHA immediately before publication.

Edits are written to a mode-0600 temporary file in the destination directory, flushed with `fsync`, and atomically replace the original only after validation succeeds. Existing rwx permission bits are restored only immediately before publication. A concurrent external modification causes the edit to fail without overwriting that external change.

## Audit behavior

The Gateway requires the configured pre-mutation audit gate before dispatching an edit. Audit records include document path, document kind, operation type, range/sheet or replacement count, byte count, and rejection state. Old/new DOCX text, XLSX cell values, and expected SHA values are not written to audit records.

## XLSX compatibility boundary

XLSX editing uses `openpyxl` for workbook-aware updates. Loading/saving is performed without formula evaluation or network fetching. If openpyxl reports an unsupported feature that would be removed, the temporary output is rejected and the original workbook is left unchanged.

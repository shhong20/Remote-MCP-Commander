# Unified multi-file reader

`read_multiple_files` reads mixed local files from one Agent call surface while preserving the existing per-format safety boundaries.

## Routing

Each input is routed by its final extension, case-insensitively:

- `.pdf`, `.docx`, `.xlsx` -> existing `preview_document` Gateway/Agent path
- `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp` -> existing `preview_image` Gateway/Agent path
- every other extension -> existing bounded UTF-8 `read_file` path

No new Agent filesystem authority is introduced. Allowed-root checks, symlink handling, binary rejection, document archive/XML limits, image signature validation, and audit behavior remain owned by the existing readers.

## Bounds

- at most 8 input files per call
- concurrency from 1 to 4, default 4
- text read defaults to 32,768 bytes and is capped at 65,536 bytes per item
- document output defaults to 32,768 characters and is capped at 65,536 characters per item
- image files keep the existing 1 MiB per-image limit
- native image content returned by one mixed call has an additional fixed 2 MiB aggregate budget

The aggregate image budget is checked before base64 decode for an item that would exceed the remaining response budget. Such an item is returned as a bounded error text block while other inputs continue.

## Result ordering and failures

Results remain in input order. Text and document items return one text content block. Image items return one metadata text block followed by one native MCP image block.

A Gateway HTTP failure, Gateway transport error, response-validation failure, or per-format rejection affects only that item and is rendered as a bounded error text block. Other files are not cancelled. Transport and malformed-response errors use generic bounded messages rather than exposing connection internals.

## Format-specific options

Each input supports the existing document navigation fields so mixed reads can still select PDF pages or an XLSX region:

- `page`, `max_pages`
- `sheet`, `cell_range`, `max_rows`
- `max_chars`
- `max_bytes` for text reads

Options irrelevant to the detected file type are ignored by the router rather than creating a second parsing implementation.

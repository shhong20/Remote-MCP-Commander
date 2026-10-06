# PDF page composition

`compose_pdf_pages` creates one PDF from bounded page ranges of existing PDFs inside Agent allowed roots.

## Contract

- Output path must be an absolute `.pdf` path inside an allowed root.
- Up to 16 source specifications are accepted.
- Each source selects `start_page` through `end_page` (inclusive); omitted `end_page` means the source's last page.
- The composed result is limited to 200 pages.
- Source PDFs are limited by the existing 32 MiB document input limit.
- Extracted temporary page data is bounded to 64 MiB and checked after every page extraction.
- The final PDF must remain within the existing 32 MiB document limit.
- The operation has a 75 second Agent-side deadline.

## Safety

The Agent uses fixed Poppler binaries only: `/usr/bin/pdfinfo`, `/usr/bin/pdfdetach`, `/usr/bin/pdfseparate`, and `/usr/bin/pdfunite`. No caller-supplied executable or shell fragment is accepted. On Debian/Ubuntu these binaries are provided by `poppler-utils`; composition fails closed when any required binary is unavailable.

Before composition, each source is inspected and rejected when JavaScript or embedded file attachments are present. Encrypted/malformed PDFs that Poppler cannot inspect are also rejected.

Sources and the output path remain subject to the existing allowed-root boundary and final symlinks are rejected. Sources are SHA-256/fingerprint snapshotted and revalidated after extraction and again before publication.

For a new output, publication uses an atomic no-overwrite hard link. Overwriting requires the current output SHA-256 and revalidates the target immediately before atomic replacement. Output mode is preserved for guarded overwrite; new files default to mode `0600`.

The temporary directory is created beside the output and restricted to mode `0700`. Temporary files are deleted on success or failure.

## Audit

Gateway required-audit succeeds before the mutation is dispatched. Audit records contain output path, source count, overwrite flag, page count, bytes written, and rejection state. Source page contents, source paths, and SHA-256 guard values are not copied into audit metadata.

## Example

Compose pages 2-4 from one PDF and page 1 from another:

```text
compose_pdf_pages(
  agent_id="server-01",
  output_path="/home/ubuntu/combined.pdf",
  sources=[
    {path: "/home/ubuntu/a.pdf", start_page: 2, end_page: 4},
    {path: "/home/ubuntu/b.pdf", start_page: 1, end_page: 1},
  ],
)
```

To replace an existing output, preview/read it first and pass its current SHA-256 with `overwrite=true`.

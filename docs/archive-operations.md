# Bounded archive operations

Remote MCP Commander v0.73 adds structured ZIP/TAR archive operations without shell extraction.

## Tools

- `inspect_archive`: validate and list a bounded archive.
- `extract_archive`: extract into a new directory.
- `create_archive`: create an archive from a previously fingerprinted directory tree.

Supported formats are `.zip`, `.tar`, `.tar.gz`, and `.tgz`.

## Safety boundaries

- archive input: 256 MiB maximum
- uncompressed content: 256 MiB maximum
- entries: 5,000 maximum
- listing returned to MCP: first 200 entries
- absolute paths, `..`, Windows drive-qualified paths, duplicate normalized paths, symlinks, hardlinks, devices, FIFOs, and encrypted ZIP entries are rejected
- extraction is written into a private temporary directory and renamed into place only after the source archive is revalidated
- extract destination must not already exist
- archive creation requires the current `tree_sha256` from `inspect_tree`
- archive output may not be inside the source tree
- overwrite requires the current archive SHA-256
- source tree and archive/output identity are revalidated before publication
- mutation routes use required audit before Agent dispatch
- archive contents, source tree path, archive input path, and SHA guards are not copied into audit payloads

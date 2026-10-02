# Binary file transfer

`read_binary` and `write_binary` provide explicit bounded binary transfer inside Agent allowed roots without shell commands.

## Read

- `read_binary(agent_id, path, offset=0, max_bytes=262144)`
- returns base64 for one bounded chunk
- maximum chunk size: 256 KiB
- returns total file size, `next_offset`, `eof`, and whole-file SHA-256
- callers can page until `eof=true` and verify the reconstructed bytes with SHA-256

## Write

- `write_binary(agent_id, path, data_base64, overwrite=false, expected_sha256=null)`
- base64 is validated by the Agent before writing
- decoded payload is bounded by `COMMANDER_FILE_MAX_BYTES` (maximum configurable value: 1 MiB)
- empty binary files are supported
- new-file creation uses an atomic no-overwrite hard-link step
- overwrite requires the current SHA-256 and rechecks it immediately before replacement
- writes are staged in the destination directory, fsynced, and atomically replaced

## Filesystem safety

- paths must be absolute and remain inside configured Agent roots
- final-path symlinks are rejected
- reads use `O_NOFOLLOW` when available and accept only regular files
- new files default to mode `0600`; overwrite preserves the previous mode
- binary operations do not widen filesystem roots or bypass existing Gateway/Agent authority

## Transport bounds

The Agent WebSocket currently accepts messages up to 2 MiB. A maximum 1 MiB binary payload encodes to 1,398,104 base64 characters, leaving room for the surrounding protocol envelope. Binary reads use smaller 256 KiB chunks, whose maximum base64 payload is 349,528 characters.

## Audit behavior

Gateway audit records include path, offsets/size, bytes written, overwrite state, and rejection state where applicable. Raw binary bytes and base64 payloads are never written to the audit log.

Binary transfer is intentionally separate from `read_file` / `write_file`, which remain UTF-8 text-oriented APIs.

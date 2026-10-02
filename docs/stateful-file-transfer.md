# Stateful large-file transfer

Remote MCP Commander v0.66 adds Agent-owned upload/download sessions for files larger than the one-shot 1 MiB binary API limit.

## Limits

- Default total file limit: 256 MiB (`COMMANDER_TRANSFER_MAX_BYTES`).
- Hard configuration cap: 1 GiB.
- Chunk payload: at most 256 KiB before base64 encoding.
- Default inactive session TTL: 900 seconds.
- Default active sessions: 4 per Agent connection.
- Transfer request timeout: 120 seconds by default for hashing and final commit work.

The existing `read_binary` and `write_binary` tools remain available for small one-shot files.

## Upload lifecycle

1. `start_file_upload` declares the final path, byte size, and whole-file SHA-256.
2. `upload_file_chunk` sends explicit sequential offsets. A previously accepted chunk may be replayed idempotently when its bytes match the temporary file.
3. `finish_file_upload` requires the declared size and SHA-256 to match before publication.
4. `close_file_transfer` aborts an unfinished upload and removes its temporary file.

Upload bytes are written to a same-directory temporary file. New targets are published with atomic no-overwrite hard-link creation. Overwrites require `expected_sha256`, revalidate the original inode/fingerprint/hash at commit time, and then use atomic replacement.

## Download lifecycle

1. `start_file_download` opens a non-symlink regular file, bounds its total size, and computes the whole-file SHA-256 plus fixed 256 KiB chunk hashes.
2. `download_file_chunk` accepts an explicit offset and returns at most 256 KiB as base64.
3. Requested fixed chunks are re-hashed before their verified bytes are returned. This catches in-place writes even when filesystem timestamp resolution does not expose the change.
4. `close_file_transfer` releases the retained file descriptor.

Explicit offsets make download retries naturally idempotent.

## Session lifecycle

Sessions live on the Agent connection, not in the Gateway. Inactive sessions expire automatically. Agent disconnect/reconnect closes all transfer file descriptors and removes unfinished upload temporary files. `file_transfer_status` refreshes the inactivity TTL and returns upload progress or the highest verified download offset served.

## Security and audit

All paths remain subject to the configured Agent allowed roots. Final symlinks are rejected, regular-file checks use `O_NOFOLLOW` where available, and upload publication stays in the destination directory.

Gateway audit records contain transfer metadata such as path, session ID, offsets, accepted byte counts, sizes, commit state, and rejection state. Raw bytes and base64 payloads are never written to audit records.

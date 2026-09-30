# Structured file append

`append_file` appends bounded UTF-8 text to an existing regular file inside Agent allowed roots without requiring shell redirection.

The operation keeps the existing `COMMANDER_FILE_MAX_BYTES` final-file ceiling. Existing binary/NUL-bearing files are rejected. An optional `expected_sha256` can bind the append to the version previously returned by `read_file`; a stale hash fails before writing.

On POSIX, the Agent opens the resolved file with append semantics, requests an exclusive advisory file lock, validates the current contents while the descriptor is held, appends, and calls `fsync`. The response reports appended byte count, final size, and final SHA-256.

The Gateway treats append as a persistent-audit-required mutation. Audit records contain path/byte/size metadata but never appended content. New Agents advertise `file.append`; the Gateway requires that capability before dispatch so rolling upgrades fail closed.

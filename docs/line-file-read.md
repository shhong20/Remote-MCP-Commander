# Line-based text file reads

`read_file_lines` provides line-oriented pagination for bounded UTF-8 files inside Agent allowed roots.

Positive `offset` values are zero-based line indexes. `max_lines` defaults to 200 and is bounded to 1000. A negative offset means tail semantics: `offset=-30` returns the final 30 lines (up to the beginning of the file); in tail mode `max_lines` is intentionally ignored.

Responses include `total_lines`, `start_line`, `next_line`, `eof`, and SHA-256 so callers can page forward or bind a subsequent mutation to the same file version.

The existing 1 MiB file ceiling applies. Binary/NUL-bearing files, invalid UTF-8, and paths outside configured roots are rejected. New Agents advertise `file.read_lines`; the Gateway requires this capability before dispatch so older Agents fail closed.

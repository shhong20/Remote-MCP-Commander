# Large-file tail reads

`tail_file` reads the final bounded UTF-8 lines from a file without loading or hashing the whole file.

- `lines`: 1..1000, default 100
- `max_bytes`: 4 KiB..1 MiB, default 256 KiB
- scan cost is bounded by `min(file_size, max_bytes)`
- responses include file size, requested/returned line counts, scanned bytes, and `truncated`

The Agent reads backwards from the initially opened regular-file snapshot. If enough complete lines are found before the byte budget is exhausted, only those final lines are returned. If the scan budget ends inside a long line, that incomplete prefix is discarded; `truncated=true` reports that the requested tail could not be fully satisfied.

This operation intentionally has no whole-file SHA-256. Calculating one would turn a tail request on a multi-gigabyte log into an O(file size) operation. Use the bounded metadata in the response rather than treating this as a mutation precondition.

Paths must remain inside configured Agent roots. Final symlinks and non-regular files are rejected. POSIX opens use `O_NOFOLLOW` and `O_NONBLOCK` where available, and the Agent checks file identity before and after open. NUL-bearing or invalid UTF-8 data in the scanned tail window is rejected.

Existing `read_file` and `read_file_lines` behavior is unchanged. New Agents advertise `file.tail`; the Gateway requires that capability before dispatch so older Agents fail closed.
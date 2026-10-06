# Command output line pagination

`command_output_lines` adds bounded line-oriented reads for retained command-session output.

- `stream`: `stdout` or `stderr`
- `offset >= 0`: zero-based line cursor
- `offset = -N`: return the final `N` available lines, up to 1000
- `max_lines`: 1..1000 for non-tail reads
- response includes `total_lines`, `start_line`, `next_line`, `eof`, `pending_partial`, and truncation state

While a command is still running, a final unterminated line is excluded from pagination and reported with `pending_partial=true`. This keeps line indexes stable if more bytes arrive on that same line. Once the session is terminal, the final unterminated line becomes a normal line.

The original character-cursor `command_output` API remains unchanged.

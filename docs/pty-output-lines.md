# PTY output line pagination

`pty_output_lines` provides bounded LF-delimited range and tail reads over retained PTY output.

- `offset >= 0`: zero-based line cursor
- `offset = -N`: final N available lines, up to 1000
- `max_lines`: 1..1000 for range reads
- response reports total/start/next lines, EOF, `pending_partial`, and truncation state

Command and PTY line pagination share one LF-only pager. Carriage returns (`\r`) are preserved inside a line instead of being counted as new lines, which is important for progress bars and terminal redraws.

While a PTY is running, its final unterminated line is withheld and reported through `pending_partial=true`. Terminal sessions include that final line after completion.

The existing character-cursor `pty_output` API remains unchanged.

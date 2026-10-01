# Output line long polling

`command_output_lines` and `pty_output_lines` accept `wait_ms` for bounded long polling when a caller has consumed all currently stable lines.

- `wait_ms=0` keeps the existing immediate behavior.
- `wait_ms` is bounded to `0..10000` milliseconds.
- positive line cursors may wait for a new stable LF-delimited line.
- negative tail offsets return immediately and never long-poll.
- a terminal session returns immediately, including its final unterminated line.
- retained-output truncation returns immediately because no additional pageable output can be stored.

Responses include `waited_ms` and `wait_timed_out`. A timeout means the requested wait budget expired while the session was still running without a new stable pageable line. Truncation and terminal completion are not reported as wait timeouts.

The Agent uses per-session `asyncio.Event` notifications instead of fixed-interval polling. Output arrival, output truncation, and terminal state transitions wake waiting readers. The wait helper clears and then rechecks state before blocking to avoid lost wakeups.

New Agents advertise `command.output_wait`. The Gateway requires that capability only when `wait_ms > 0`, so older Agents continue to serve existing immediate line reads while long-poll requests fail closed instead of being silently ignored.
Long-poll requests are dispatched outside the Agent receive loop, so a waiting output read does not block ping, file, command, or other control traffic. The dispatcher bounds concurrent waits to 16 and cancels outstanding waits when the Agent connection closes.

Command-session stdout and stderr use independent wake events. Heavy stdout traffic therefore does not repeatedly wake a caller that is waiting only for stderr. PTY sessions use their single combined output event.
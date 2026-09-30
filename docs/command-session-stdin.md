# Command session stdin

Command sessions support bounded structured stdin without requiring a PTY.

`write_command_input(agent_id, session_id, data)` writes UTF-8 input to a running command session. Each call is bounded by `COMMANDER_SESSION_INPUT_MAX_BYTES` (16 KiB by default, hard max 64 KiB). `close_command_stdin(agent_id, session_id)` closes the pipe and delivers EOF; closing an already-closed stdin is idempotent.

Command sessions start with a stdin pipe. Writes are accepted only while the session is running and stdin remains open. Missing sessions, completed sessions, broken pipes, and oversized input fail closed.

The Gateway requires the `command.stdin` capability before dispatch. Persistent audit preflight records only session ID and input byte count; input contents are never stored in the audit journal. The Agent independently enforces the byte limit.

Session timeout and cancellation also use the common process-group TERM/KILL cleanup path on POSIX so descendants created by shell commands are reaped with the parent.

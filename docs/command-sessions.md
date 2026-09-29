# Command sessions

Command sessions provide a cancellable, pollable execution lifecycle without exposing an interactive shell.

## MCP lifecycle

- `start_command(agent_id, argv)` returns a 32-hex `session_id` and an initial snapshot.
- `command_status(agent_id, session_id)` returns current state and bounded stdout/stderr.
- `cancel_command(agent_id, session_id)` requests graceful termination and escalates to process kill only if the process does not exit within the short cancellation grace period.

States are `running`, `completed`, `cancelled`, `timed_out`, or `failed`.

## Security boundary

Sessions use the same fixed generic command policy as one-shot `execute` and do not accept shell command strings. Only bare executable names are accepted. The Agent resolves them through `/usr/bin:/bin` and also fixes the child `PATH` to that trusted search path. An absolute path such as `/tmp/uptime`, an interpreter, shell, or `systemctl` is therefore rejected even if its basename appears operator-approved.

Current safe generic profiles are `echo`, argument-free `hostname`, `whoami`, and `uptime`. This initial session feature is infrastructure for cancellation/state management; widening the executable surface requires a separate policy and approval design.

## Lifetime and limits

Sessions are Agent-connection scoped, not durable jobs. The Agent cancels running sessions when its WebSocket handler tears down. Completed snapshots are retained only in bounded in-memory history for that connection.

Configuration:

- `COMMANDER_SESSION_TIMEOUT_S` — max runtime, default 300 seconds.
- `COMMANDER_SESSION_MAX_ACTIVE` — max concurrent running sessions, default 4.
- `COMMANDER_SESSION_HISTORY_LIMIT` — bounded in-memory session history, default 100 and required to be at least the active limit.
- `COMMANDER_MAX_OUTPUT_BYTES` — bounded stdout and stderr per stream; configuration is capped at 256 KiB.

This is not a PTY: there is no stdin streaming, shell expansion, terminal emulation, detached background job, or reconnect-to-running-session behavior.

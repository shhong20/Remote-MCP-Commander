# Runtime session signals

`signal_session` sends a bounded process signal to a managed command or PTY session.

- supported signals: `term`, `kill`, `int`, `hup`
- caller supplies `session_id` and `kind`, not a raw PID
- the Agent resolves the session to its stored PID and `create_time_ms`
- process identity is re-read immediately before signaling to reject PID reuse or unreadable identity
- POSIX sessions target the managed process group so shell descendants receive the signal consistently

The capability is advertised only in Personal mode when process approvals are not required. Hardened mode, or Personal mode with `COMMANDER_PERSONAL_PROCESS_APPROVAL_REQUIRED=true`, does not expose `command.session_signal`.

Gateway dispatch is also mode-gated and requires persistent audit preflight. The operation therefore cannot be enabled merely by forging a capability advertisement.

This is additive to the existing guarded `signal_process` tool. Use `signal_session` when the target is already a managed Remote MCP Commander command/PTTY session; use `signal_process` for a separately identified process with explicit PID/create-time identity.

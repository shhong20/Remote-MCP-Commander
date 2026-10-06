# Runtime session process identity

Command and PTY session snapshots now expose the operating-system process identity that backs each retained session.

- `pid`: the spawned process ID.
- `create_time_ms`: the process creation timestamp in milliseconds, using the same `psutil` basis as `list_processes` and process mutation guards.
- `list_sessions` returns the same identity for active and retained completed sessions.

This lets an operator recover a session, correlate it directly with `list_processes`, and pass the matching `pid` plus `create_time_ms` into guarded process signal or termination flows without guessing which reused PID is intended.

The identity is captured immediately after process creation and retained with the session record. If the process exits before its creation time can be read, `create_time_ms` remains null rather than inventing an identity.

The fields are additive and optional. Responses from older Agents that do not contain them still validate with `pid=null` and `create_time_ms=null`, preserving rolling-upgrade compatibility.

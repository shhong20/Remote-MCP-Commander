# Runtime session listing

`list_sessions` recovers command and PTY session IDs and provides a bounded operational overview without embedding retained output in the response.

```text
list_sessions(agent_id="server-01")
list_sessions(agent_id="server-01", kind="command")
list_sessions(agent_id="server-01", kind="pty", include_completed=true, limit=50)
```

By default only running sessions are returned. Set `include_completed=true` to include retained terminal history that has not been discarded or pruned. `kind` accepts `all`, `command`, or `pty`, and `limit` is bounded to 200.

Each row contains session ID, session kind, executable, state, cwd, start/finish timestamps, return code, retained output character count, and whether output retention was truncated. Output text itself remains available only through the existing `command_output` or `pty_output` tools.

The Agent advertises `command.session_list` when either command sessions or PTY sessions are available. The Gateway requires that capability before dispatching the list request, so older Agents cannot silently ignore the operation. Combined command/PTY results are sorted newest-first by session start time.

# Interactive PTY sessions

Interactive PTY sessions provide bounded terminal input/output for explicitly approved POSIX workflows. They are a separate execution surface from generic `execute` and command sessions and are disabled by default.

## Security boundary

A PTY start succeeds only when all of these checks pass:

1. The connected Agent advertises `command.pty`.
2. The executable is a bare name resolved only through `/usr/bin:/bin`.
3. The Gateway's per-Agent `COMMANDER_PTY_AGENT_POLICIES_JSON` contains the executable.
4. The Agent's `COMMANDER_PTY_ALLOWED_EXECUTABLES` contains the executable.
5. A short-lived, one-use external approval for `pty.start` matches the exact canonical argv hash.

The normal generic executable policy does not grant PTY access. Absolute paths are rejected. No shell command string is accepted, but explicitly allowing a shell grants the Agent OS user's shell authority after approval. Run the Agent as an unprivileged user and keep both PTY allowlists narrow.

PTY sessions are POSIX-only. They receive a controlling terminal, foreground process group, bounded UTF-8 input, bounded retained output, a session timeout, an active-session limit, and terminal dimensions restricted to 20-500 columns and 5-200 rows.

## Configuration

Gateway:

```env
COMMANDER_PTY_AGENT_POLICIES_JSON={"server-01":["bash"]}
COMMANDER_PTY_INPUT_MAX_BYTES=16384
```

Agent:

```env
COMMANDER_PTY_ALLOWED_EXECUTABLES=bash
COMMANDER_PTY_TIMEOUT_S=900
COMMANDER_PTY_MAX_ACTIVE=1
COMMANDER_PTY_INPUT_MAX_BYTES=16384
```

Keep the Gateway and Agent input limits aligned. Output uses the existing `COMMANDER_MAX_OUTPUT_BYTES`; completed PTYs share `COMMANDER_SESSION_HISTORY_LIMIT`.

## Issue an exact-argv approval

Generate the target on the operator host using the exact argv that will be sent:

```bash
PTY_TARGET="$(remote-mcp-pty-target -- bash -l)"
curl -X POST \
  -H "Authorization: Bearer $COMMANDER_APPROVAL_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"agent_id\":\"server-01\",\"operation\":\"pty.start\",\"target\":\"$PTY_TARGET\"}" \
  http://127.0.0.1:8765/api/v1/approvals
```

Pass the returned `approval_id` and `approval_secret` to `start_pty` together with the same `["bash", "-l"]` argv. The Gateway consumes the approval before dispatch. Any timeout, disconnect, argument change, or replay requires a new approval.

Approval issuance remains outside MCP, and the approval-admin credential must never be placed in MCP client configuration.

## Session lifecycle

- `start_pty` returns a connection-scoped session ID.
- `write_pty` accepts bounded UTF-8 input; callers include newline or control characters explicitly.
- `pty_output` uses a character cursor so callers can fetch only new retained output.
- `resize_pty` updates the kernel PTY dimensions.
- `cancel_pty` terminates the PTY process group, escalating from TERM to KILL after a short grace period.
- `discard_pty` removes only a terminal session from Agent history.

All running PTYs are cancelled when the owning Agent WebSocket disconnects. Sessions cannot detach, survive Agent restart, or reconnect across a new Agent connection.

## Audit and data handling

Start, input, resize, cancel, and discard operations emit audit metadata. Audit records include Agent/session IDs, executable, argument count, dimensions, and input byte count where applicable. They never include argv values, input content, PTY output, approval secrets, or environment values.

PTY output can still contain secrets and is returned to the authenticated caller. Retention is an in-memory prefix capped by `COMMANDER_MAX_OUTPUT_BYTES`; once truncated, later output is drained but not retained. This boundary is application-level defense in depth, not an OS sandbox.

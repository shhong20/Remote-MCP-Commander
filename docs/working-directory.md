# Working directory support

Remote MCP Commander v0.32.0 allows `execute`, `start_command`, and `start_pty` to accept an optional `cwd`.

The Agent resolves the requested working directory before process creation and requires the canonical directory to stay inside one of the configured allowed roots. Relative paths and allowed-root escapes are rejected. A symlink that resolves outside an allowed root is therefore rejected as well.

New Agents advertise the `command.cwd` capability when filesystem roots are available. The Gateway requires that capability whenever a caller supplies `cwd`, preventing older Agents from silently ignoring the new field during rolling upgrades.

For PTY sessions, the one-use approval target binds both the exact argv and the exact requested cwd. Omitting cwd preserves the legacy argv-only approval target for backward compatibility.

Example MCP calls:

```text
execute(agent_id="server-01", argv=["pytest", "-q"], cwd="/home/ubuntu/project")
start_command(agent_id="server-01", argv=["npm", "test"], cwd="/home/ubuntu/project/web")
```

Command-session cancellation also terminates the POSIX process group so descendants created by shell commands do not survive cancellation or timeout.

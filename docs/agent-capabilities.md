# Agent capabilities

Each Agent advertises a bounded capability list in its authenticated `hello` message. The Gateway exposes the list through `list_devices` and `device_info` so MCP clients can avoid trial-and-error calls on unsupported hosts.

Capability names are limited to 64 entries and 64 characters per entry using a lowercase dot-namespace format.

## Detection

Capabilities are derived locally when the Agent connection starts.

Always-available application paths include device ping, system health, TCP port lookup, process listing, and process termination. They still run with the Agent OS user's normal permissions.

`service.status` and `service.action` are advertised only when `systemctl` is available in the trusted system path. `service.logs` requires `journalctl`.

Filesystem read/write/discovery capabilities require at least one configured allowed root. `git.status` additionally requires Git in the trusted path.

Safe command execution/session capabilities require at least one configured safe-profile executable to resolve through the fixed trusted command path.

## Interpretation

A capability means the Agent can expose that operation path. It is not a promise that every target is accessible. For example, `process.terminate` may still fail against a process owned by another OS user, and `service.action` may still be denied by systemd/OS permissions.

Gateway policy remains authoritative in addition to Agent capability advertisement. Capability discovery never bypasses command allowlists, allowed roots, enrollment authentication, or external mutation approvals.

`command.pty` is advertised only on POSIX when at least one executable in the dedicated PTY Agent allowlist resolves through the fixed trusted path. It does not imply that Gateway PTY policy or an external start approval exists.

Older Agents that omit the field remain compatible and appear with an empty capability list. This allows rolling upgrades without changing the existing WebSocket protocol all at once.

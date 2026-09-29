# Remote MCP Commander

Self-hosted remote command bridge for AI/MCP clients.

> Status: early MVP. Keep the Gateway and MCP endpoint on localhost/private networks until TLS, enrollment, and production authorization are configured.

## Architecture

```text
MCP Client (ChatGPT / Claude / Inspector)
        |
        | MCP: stdio or Streamable HTTP
        v
Remote MCP Commander MCP Server
        |
        | authenticated control API
        v
Remote MCP Gateway
        ^
        | outbound persistent WebSocket
        |
Remote Agent(s)
```

Agents initiate outbound connections, so controlled hosts do not need inbound SSH or a public agent port.

## Current MVP

- MCP Python SDK v2 tool server
- MCP tools for devices, bounded files, cancellable command/PTY sessions, system inspection, and approval-gated mutations
- Streamable HTTP for deployed MCP access; stdio for local MCP clients
- MCP bearer-token verification for Streamable HTTP
- Persistent outbound Agent -> Gateway WebSocket
- One-time device enrollment, hashed registry credentials, and revocation
- Per-agent token support and Gateway-side host policies
- Agent-side executable allowlist plus fixed safe generic command profiles
- Separate operator-only, one-use mutation approvals bound to Agent, action, target, and TTL
- `create_subprocess_exec(..., shell=False)` command execution
- Request/response correlation with request IDs
- Actual Gateway -> Agent RTT ping
- Agent metadata, heartbeat, `last_seen`, negotiated protocol version, and runtime capability advertisement
- Command timeout and bounded stdout/stderr
- SHA-256 chained JSONL audit journal with redaction, bounded retention, verification, and optional remote shipping
- Native systemd + Caddy TLS deployment templates with startup preflight doctor
- Offline `remote-mcp-doctor` role preflight wired into systemd startup
- Domain-separated Ed25519 publication signatures for package manifests and runtime locks
- Atomic, content-addressed local registry for trusted package/runtime artifact sets
- Fixed native-stack activation with bounded health checks and automatic trusted rollback
- Approval-gated POSIX PTY sessions under separate deny-by-default Gateway and Agent policy
- Ruff + pytest CI

## Quick start

Requires Python 3.11+.

```bash
git clone https://github.com/shhong20/Remote-MCP-Commander.git
cd Remote-MCP-Commander
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

Replace every placeholder credential before use. `COMMANDER_APPROVAL_ADMIN_TOKEN` is operator-only and must be different from the Gateway control, MCP, and Agent credentials.

Start the Gateway:

```bash
remote-mcp-gateway
```

## Enroll a device

Create a one-time pairing code from the authenticated control API, then run `remote-mcp-enroll` on the controlled machine with the returned code. Remote enrollment requires HTTPS; plain HTTP is accepted only for loopback development. Remote Agent WebSockets likewise require `wss://`, except on loopback.

The issued Agent credential is stored locally with owner-only permissions, while the Gateway registry stores only its SHA-256 hash. Pairing codes are one-time and expire; the current MVP keeps pending pairing codes in memory, so a Gateway restart invalidates them.

Revocation closes an active connection and prevents the old credential from reconnecting. Static Agent tokens remain available for migration, but once an Agent has a registry identity the registry is authoritative and static fallback is disabled.

## MCP server

For a local MCP host, use stdio:

```bash
COMMANDER_MCP_TRANSPORT=stdio remote-mcp-server
```

For a deployed MCP endpoint, use Streamable HTTP and configure a long random MCP token:

```bash
COMMANDER_MCP_TRANSPORT=streamable-http remote-mcp-server
```

The endpoint defaults to `http://127.0.0.1:8766/mcp`. Streamable HTTP refuses to start without a sufficiently long `COMMANDER_MCP_TOKEN`.

The current MCP tools are intentionally narrow:

- `list_devices()` - connected devices and metadata
- `device_info(agent_id)` - one device
- `ping_device(agent_id)` - real Gateway/Agent round-trip latency
- `system_health(agent_id)` - bounded CPU, memory, load, uptime, swap, and root-disk metrics
- `lookup_port(agent_id, port)` - find bounded TCP listeners without exposing process command lines
- `service_logs(agent_id, unit, lines)` - bounded recent journal entries for one validated systemd unit
- `git_status(agent_id, path)` - bounded porcelain-v2 status for a normal Git repo inside allowed roots
- `list_file_roots(agent_id)` - show filesystem roots explicitly exposed by the Agent
- `list_directory(agent_id, path, limit)` - bounded, non-recursive directory listing inside allowed roots
- `file_info(agent_id, path)` - lstat-style metadata without following the final symlink
- `execute(agent_id, argv)` - structured argv execution under fixed safe profiles
- `start_command(agent_id, argv)` - start a connection-scoped cancellable safe-profile command session
- `command_status(agent_id, session_id)` - read current session state and retained bounded output
- `command_output(agent_id, session_id, stdout_offset, stderr_offset, max_chars)` - fetch only new output using character cursors
- `cancel_command(agent_id, session_id)` - request TERM, then KILL after a short grace period if needed
- `discard_command(agent_id, session_id)` - explicitly remove a completed session from Agent history
- `start_pty(...)` - start a POSIX PTY after consuming an external approval bound to the exact argv
- `pty_status(...)`, `pty_output(...)` - read bounded PTY state/output
- `write_pty(...)`, `resize_pty(...)` - send bounded input or update terminal dimensions
- `cancel_pty(...)`, `discard_pty(...)` - terminate or remove a connection-scoped PTY session
- `read_file(agent_id, path, offset, max_bytes)` - bounded text reads inside configured roots
- `write_file(agent_id, path, content, overwrite, expected_sha256)` - bounded atomic text writes
- `list_processes(agent_id, limit)` - bounded process metadata without command lines/environments
- `service_status(agent_id, unit)` - read-only systemd state via fixed `systemctl show` arguments
- `terminate_process(...)` - approval-gated process termination with process creation-time identity checking
- `service_action(...)` - approval-gated `start`, `stop`, or `restart` for one exact systemd unit

`execute` never accepts a shell command string. It is also limited to fixed safe generic profiles: `echo`, argument-free `hostname`, `whoami`, and `uptime`. Generic execution accepts bare executable names only, resolves them from the fixed trusted search path `/usr/bin:/bin`, and gives the child the same restricted `PATH`. Adding `systemctl`, a shell/interpreter, another executable, or an absolute-path alias to configuration therefore does not bypass the mutation approval path. Gateway policy and Agent policy must both allow the executable.

Protocol compatibility is negotiated during the initial Agent hello. A WebSocket is not promoted into the active device registry until hello identity and protocol-range validation succeed, so an incompatible replacement cannot evict a healthy Agent. Legacy hello messages without protocol fields are treated as protocol v1. See `docs/protocol-compatibility.md`.

Connected Agents advertise a bounded capability set derived from their real local environment (for example files, Git, systemd, journal logs, process inspection, and safe command sessions). `list_devices` and `device_info` include this list so MCP clients can choose supported tools before attempting a call. Capabilities describe an available Agent-side path, not guaranteed OS permission for every target. See `docs/agent-capabilities.md`.

Read-only diagnostics do not widen generic execution. Port lookup uses psutil without returning process command lines or environments. Service logs call a fixed trusted-path `journalctl` argv with validated unit names and bounded lines/output. Git status is restricted to normal `.git/` directory repositories fully inside configured allowed roots; gitfile/worktree and symlink metadata are rejected, and fsmonitor/hooks/global configuration/pagers are disabled for the fixed status command. See `docs/read-only-diagnostics.md`.

Command sessions use the exact same generic policy. They persist across MCP calls while the Agent WebSocket connection is alive, incrementally drain bounded stdout/stderr, support cursor-based output polling without retransmitting already-consumed text, enforce active/history limits and a session timeout, and are cancelled when that connection is torn down. Terminal states are published only after output readers finish, so a completed snapshot cannot race ahead of its final retained output. They are not interactive PTYs and do not widen the executable surface. See `docs/command-sessions.md`.

Interactive PTY sessions are a separate POSIX-only surface and are disabled by default. Starting one requires an exact-argv, one-use external approval plus matching `COMMANDER_PTY_AGENT_POLICIES_JSON` Gateway policy and `COMMANDER_PTY_ALLOWED_EXECUTABLES` Agent allowlist. Input content and output are never copied into the audit journal; only bounded metadata is recorded. See `docs/interactive-pty.md`.

Filesystem discovery uses the same allowed-root boundary. Directory listing is non-recursive and bounded to 500 entries. Entries expose only name/path/type, regular-file size, and modification time; symlink targets are not followed or returned. `file_info` uses lstat-style metadata for the final path, so an allowed-root symlink cannot reveal its target metadata/content. See `docs/filesystem-discovery.md`.

File tools are deny-by-default until `COMMANDER_ALLOWED_ROOTS_JSON` is configured with absolute directories. Paths are canonicalized before access so symlink escapes outside those roots are rejected. Existing files require `overwrite=true` plus the SHA-256 returned by a prior `read_file`, providing optimistic stale-write protection. The current text-file MVP caps files at 1 MiB and does not expose delete, move, recursive directory, or arbitrary binary transfer operations.

## Native deployment

For a long-running host deployment, keep Gateway and MCP services bound to `127.0.0.1` and place Caddy in front for HTTPS/WSS. The repository includes hardened systemd service templates for Gateway/MCP plus a systemd **user** service for each controlled-host Agent.

Gateway and MCP run under separate unprivileged `remote-mcp-gateway` and `remote-mcp-server` accounts. Agents should run as the ordinary OS user whose files/processes are intentionally exposed; do not run an Agent as root just to gain visibility. Deployment environment examples intentionally contain no credentials.

Use `remote-mcp-doctor gateway|mcp|agent` to validate the effective deployment environment; `--json` is available for automation. The systemd templates run the role-specific doctor as `ExecStartPre`. See `docs/deployment-doctor.md`.

Prepared releases are sealed with an exact-tree SHA-256 manifest, signed with Ed25519, verified against public keys outside the release tree, switched through atomic `current`/`previous` symlinks, and can be rolled back only to another trusted signed release. Ordinary activation never restarts services; the explicit `activate-checked` workflow restarts only the fixed native Gateway/MCP stack and automatically restores the previous trusted release on failed health. See `docs/release-lifecycle.md`, `docs/release-integrity.md`, `docs/release-signing.md`, and `docs/health-rollback.md`. Reproducible source/wheel transport artifacts can be generated from an exact Git commit with `remote-mcp-package`; the builder self-checks byte-for-byte reproduction under a pinned toolchain, and package manifests can be signed in a separate Ed25519 publication trust domain. Trusted packages can then be resolved into an exact wheelhouse with a separately signed runtime lock and atomically published as a content-addressed artifact set. See `docs/reproducible-packaging.md`, `docs/package-publication.md`, `docs/runtime-input-locks.md`, `docs/runtime-publication.md`, and `docs/artifact-registry.md`.

See `docs/native-deployment.md` and `deploy/`.

Before starting a service manually, run `remote-mcp-doctor gateway`, `remote-mcp-doctor mcp`, or `remote-mcp-doctor agent`. The provided systemd templates run the matching preflight automatically with `ExecStartPre`. Doctor output reports only validation status/details and never prints configured credential values. See `docs/deployment-doctor.md`.

## Mutation approvals

Approval creation is deliberately outside the MCP tool surface. An operator uses the Gateway `POST /api/v1/approvals` endpoint with the separate `COMMANDER_APPROVAL_ADMIN_TOKEN`. The request binds an approval to one `agent_id`, one operation (`process.terminate`, `service.start`, `service.stop`, `service.restart`, or `pty.start`), and one exact target.

The Gateway returns an approval ID and short-lived secret. It stores only the secret hash. The pair can be consumed once; concurrent replay attempts allow only one consumer. Wrong secrets or mismatched Agent/action/target values do not consume the legitimate grant. Pending approvals are in-memory in this MVP, so Gateway restart invalidates them.

Process approvals use the canonical target `pid:<pid>@<create_time_ms>`, where `create_time_ms` comes from `list_processes`. The Agent rechecks that process creation time immediately before termination so a reused PID cannot authorize a different process. It requests graceful termination only and never escalates to a hard kill.

Service approvals bind the exact unit name and action. The Agent executes a fixed `systemctl <action> <unit> --no-pager` argv without a shell and never invokes `sudo`, so the operation is limited to the Agent OS user's existing privileges.

PTY approvals bind the SHA-256 of a canonical exact argv. Generate the target with `remote-mcp-pty-target -- <executable> [arguments...]`; changing even one argument requires a new approval.

The Gateway consumes the approval before dispatching the mutation. This gives at-most-once behavior: after a timeout or disconnect, a fresh operator approval is required instead of replaying the old authorization.

## Control API

Read-only/control endpoints use `COMMANDER_CONTROL_TOKEN`. Approval issuance uses the separate operator-only approval credential. Do not expose the Gateway directly to an untrusted network; use TLS/reverse-proxy protection for non-loopback deployment.

Recent persistent audit records are available only through the control API, not through MCP:

```bash
curl -H "Authorization: Bearer $COMMANDER_CONTROL_TOKEN" \
  'http://127.0.0.1:8765/api/v1/audit?limit=100&agent_id=server-01'
```

Every new journal record carries a chain ID, monotonic sequence, previous hash, and canonical SHA-256 record hash. The Gateway verifies retained history at startup, rotates by configured size, queries across retained files, and can ship the already-redacted chained record to a fixed HTTPS collector. Use `remote-mcp-audit verify <path>` for offline checks or the authenticated `GET /api/v1/audit/verify` endpoint while the Gateway is running. See `docs/persistent-audit.md`.

## Security principles

1. Deny by default.
2. Keep execution and approval policy outside the model.
3. Require both Gateway and Agent policy checks.
4. Separate operator approval credentials from MCP/control credentials.
5. Keep credentials out of the repository.
6. Prefer outbound Agent connections.
7. Bound command runtime and output size.
8. Run Agents as unprivileged OS users.
9. Require TLS for every non-loopback Agent, Gateway-control, and Streamable HTTP connection.
10. Treat application policy as defense in depth, not as an OS sandbox.

## Planned next

- multi-user RBAC and stronger OAuth/OIDC integration
- signed/versioned policy distribution to Agents

## Development

```bash
ruff check .
pytest -q
```

The implementation uses FastAPI, WebSockets, MCP Python SDK v2, Pydantic, HTTPX, psutil, and asyncio.

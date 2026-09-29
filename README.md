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
- MCP tools for devices, bounded files, system inspection, and approval-gated mutations
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
- Agent metadata, heartbeat, and `last_seen`
- Command timeout and bounded stdout/stderr
- Structured audit events
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
- `execute(agent_id, argv)` - structured argv execution under fixed safe profiles
- `read_file(agent_id, path, offset, max_bytes)` - bounded text reads inside configured roots
- `write_file(agent_id, path, content, overwrite, expected_sha256)` - bounded atomic text writes
- `list_processes(agent_id, limit)` - bounded process metadata without command lines/environments
- `service_status(agent_id, unit)` - read-only systemd state via fixed `systemctl show` arguments
- `terminate_process(...)` - approval-gated process termination with process creation-time identity checking
- `service_action(...)` - approval-gated `start`, `stop`, or `restart` for one exact systemd unit

`execute` never accepts a shell command string. It is also limited to fixed safe generic profiles: `echo`, argument-free `hostname`, `whoami`, and `uptime`. Adding `systemctl`, a shell/interpreter, or another executable to configuration therefore does not bypass the mutation approval path. Gateway policy and Agent policy must both allow the executable.

File tools are deny-by-default until `COMMANDER_ALLOWED_ROOTS_JSON` is configured with absolute directories. Paths are canonicalized before access so symlink escapes outside those roots are rejected. Existing files require `overwrite=true` plus the SHA-256 returned by a prior `read_file`, providing optimistic stale-write protection. The current text-file MVP caps files at 1 MiB and does not expose delete, move, recursive directory, or arbitrary binary transfer operations.

## Mutation approvals

Approval creation is deliberately outside the MCP tool surface. An operator uses the Gateway `POST /api/v1/approvals` endpoint with the separate `COMMANDER_APPROVAL_ADMIN_TOKEN`. The request binds an approval to one `agent_id`, one operation (`process.terminate`, `service.start`, `service.stop`, or `service.restart`), and one exact target.

The Gateway returns an approval ID and short-lived secret. It stores only the secret hash. The pair can be consumed once; concurrent replay attempts allow only one consumer. Wrong secrets or mismatched Agent/action/target values do not consume the legitimate grant. Pending approvals are in-memory in this MVP, so Gateway restart invalidates them.

Process approvals use the canonical target `pid:<pid>@<create_time_ms>`, where `create_time_ms` comes from `list_processes`. The Agent rechecks that process creation time immediately before termination so a reused PID cannot authorize a different process. It requests graceful termination only and never escalates to a hard kill.

Service approvals bind the exact unit name and action. The Agent executes a fixed `systemctl <action> <unit> --no-pager` argv without a shell and never invokes `sudo`, so the operation is limited to the Agent OS user's existing privileges.

The Gateway consumes the approval before dispatching the mutation. This gives at-most-once behavior: after a timeout or disconnect, a fresh operator approval is required instead of replaying the old authorization.

## Control API

Read-only/control endpoints use `COMMANDER_CONTROL_TOKEN`. Approval issuance uses the separate operator-only approval credential. Do not expose the Gateway directly to an untrusted network; use TLS/reverse-proxy protection for non-loopback deployment.

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

- cancellable/persistent terminal sessions with explicit policy boundaries
- persistent audit storage and audit-query tools
- TLS/reverse-proxy deployment templates
- multi-user RBAC and stronger OAuth/OIDC integration
- signed/versioned policy distribution to Agents

## Development

```bash
ruff check .
pytest -q
```

The implementation uses FastAPI, WebSockets, MCP Python SDK v2, Pydantic, HTTPX, psutil, and asyncio.

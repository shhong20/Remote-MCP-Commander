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
- `list_devices`, `device_info`, `ping_device`, `execute` MCP tools
- Streamable HTTP for deployed MCP access; stdio for local MCP clients
- MCP bearer-token verification for Streamable HTTP
- Persistent outbound Agent -> Gateway WebSocket
- One-time device enrollment, hashed registry credentials, and revocation
- Per-agent token support and Gateway-side host policies
- Agent-side executable allowlist
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

Replace every placeholder credential before use.

Start the Gateway:

```bash
remote-mcp-gateway
```

## Enroll a device

Create a one-time pairing code from the authenticated control API:

```bash
curl -X POST \
  -H "Authorization: Bearer $COMMANDER_CONTROL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"server-01"}' \
  http://127.0.0.1:8765/api/v1/enrollments
```

Run enrollment on the controlled machine with the returned code:

```bash
remote-mcp-enroll --agent-id server-01 --gateway https://gateway.example.com --code '<pairing-code>'
remote-mcp-agent
```

Remote enrollment requires HTTPS; plain HTTP is accepted only for loopback development. The issued Agent credential is stored locally with owner-only permissions, while the Gateway registry stores only its SHA-256 hash. Pairing codes are one-time and expire; the current MVP keeps pending pairing codes in memory, so a Gateway restart invalidates them.

Revoke a registered device:

```bash
curl -X POST \
  -H "Authorization: Bearer $COMMANDER_CONTROL_TOKEN" \
  http://127.0.0.1:8765/api/v1/agents/server-01/revoke
```

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
- `execute(agent_id, argv)` - structured argv execution

`execute` never accepts a shell command string. Gateway policy and Agent policy must both allow the executable.

## Control API

List connected devices:

```bash
curl -H "Authorization: Bearer $COMMANDER_CONTROL_TOKEN" \
  http://127.0.0.1:8765/api/v1/agents
```

## Security principles

1. Deny by default.
2. Keep execution policy outside the model.
3. Require both Gateway and Agent policy checks.
4. Keep credentials out of the repository.
5. Prefer outbound Agent connections.
6. Bound command runtime and output size.
7. Run Agents as unprivileged OS users.
8. Use TLS before exposing Streamable HTTP beyond localhost/private networks.
9. Treat allowlists as guardrails, not an OS sandbox.

## Planned next

- bounded file read/write tools
- process and service management
- cancellable/persistent terminal sessions
- persistent audit storage
- TLS/reverse-proxy deployment templates
- multi-user RBAC and stronger OAuth/OIDC integration

## Development

```bash
ruff check .
pytest -q
```

The implementation uses FastAPI, WebSockets, MCP Python SDK v2, Pydantic, HTTPX, and asyncio.

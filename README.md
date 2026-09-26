# Remote MCP Commander

Self-hosted remote command and file-management bridge designed for AI/MCP clients.

> Status: early MVP. Do not expose the gateway or agent to the public Internet without TLS, authentication, and an explicit command policy.

## Goal

Remote MCP Commander separates control-plane access from the machines being controlled:

```text
AI / MCP Client
      |
      v
Remote MCP Gateway
      ^
      | outbound persistent WebSocket
      |
Remote Agent(s)
```

Agents initiate outbound connections to the gateway, so controlled hosts do not need inbound SSH or a publicly exposed agent port.

## Current MVP

- Persistent outbound Agent -> Gateway WebSocket
- Bearer-token authentication for agent and control APIs
- Connected-agent inventory
- Request/response correlation using request IDs
- Command execution using `create_subprocess_exec(..., shell=False)`
- Deny-by-default executable allowlist
- Per-command timeout
- Bounded stdout/stderr capture
- Reconnect loop for disconnected agents
- Basic CI with Ruff + pytest

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

Replace both tokens in `.env` with long random values before running anything outside localhost.

Start the gateway:

```bash
remote-mcp-gateway
```

Start an agent in another terminal:

```bash
remote-mcp-agent
```

List connected agents:

```bash
curl -H "Authorization: Bearer $COMMANDER_CONTROL_TOKEN" \
  http://127.0.0.1:8765/api/v1/agents
```

Execute an allowlisted command:

```bash
curl -X POST \
  -H "Authorization: Bearer $COMMANDER_CONTROL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"argv":["hostname"]}' \
  http://127.0.0.1:8765/api/v1/agents/server-01/execute
```

The API accepts an argv array, not a shell command string. Shell operators such as `&&`, pipes, redirects, and command substitution are therefore not interpreted by a shell.

## Security principles

1. Deny by default.
2. Never accept arbitrary shell execution unless the operator explicitly enables it.
3. Keep credentials out of the repository.
4. Prefer outbound agent connections.
5. Record who requested what, against which host, and the result.
6. Bound command runtime and output size.
7. Run the agent as an unprivileged OS user.
8. Use TLS before exposing the gateway outside localhost/private networks.

## Not implemented yet

The current branch is intentionally narrow. The following are planned but should not be considered available or secure yet:

- MCP tool endpoint for ChatGPT/other MCP clients
- per-agent credentials and scoped authorization
- bounded file read/write operations
- process/service management
- interactive terminal sessions
- structured audit persistence
- TLS/reverse-proxy deployment templates
- enrollment/revocation flow
- multi-user RBAC

## Development

```bash
ruff check .
pytest -q
```

The initial implementation uses FastAPI, WebSockets, Pydantic, and asyncio.

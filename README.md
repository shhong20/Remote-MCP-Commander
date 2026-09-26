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

## MVP scope

- Agent registration and heartbeat
- Persistent outbound WebSocket connection
- Request/response correlation
- Command execution behind an explicit allow-list policy
- Basic host/process information
- Gateway health and connected-agent inventory
- Audit-friendly request IDs

Planned next: bounded file operations, process/service controls, interactive sessions, stronger identity/authorization, TLS deployment, and an MCP-facing tool server.

## Security principles

1. Deny by default.
2. Never accept arbitrary shell execution unless the operator explicitly enables it.
3. Keep credentials out of the repository.
4. Prefer outbound agent connections.
5. Record who requested what, against which host, and the result.
6. Bound command runtime and output size.
7. Run the agent as an unprivileged OS user.

## Development

The initial implementation is Python 3.11+ using FastAPI, WebSockets, and Pydantic.

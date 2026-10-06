# Deployment doctor

`remote-mcp-doctor` is an offline preflight for the three runtime roles. It validates configuration and local filesystem prerequisites without issuing remote commands.

```bash
remote-mcp-doctor gateway
remote-mcp-doctor mcp
remote-mcp-doctor agent
```

Add `--json` for machine-readable output. Exit code is non-zero when any check fails; warnings do not fail the preflight.

## Gateway checks

The Gateway role validates credential policy, audit/registry destinations, loopback binding guidance, and whether the optional approval-admin credential is configured.

State-path checks do not create files. They reject symlink/non-regular targets and verify that an existing ancestor can be written by the current service identity.

## MCP checks

The MCP role validates the authenticated Gateway URL, Streamable HTTP token/metadata requirements, and reports a warning when a network MCP listener is not loopback-bound.

## Agent checks

The Agent role loads the configured credential, verifies owner-only token-file requirements, validates session limits, requires WSS for non-loopback Gateways, and requires the WebSocket path to end in the exact configured Agent ID.

Allowed roots are resolved using the same production helper used by file tools. The doctor then reports how many runtime capability paths the Agent would advertise.

An empty allowed-root set is a warning rather than a failure because process/system diagnostics may still be useful without filesystem access.

## Secret handling

Doctor output never intentionally includes configured credential values. URL transport validation also avoids echoing the rejected URL, preventing accidental userinfo/query secrets from being repeated in service logs.

The systemd templates run the appropriate role through `ExecStartPre`; a failed preflight therefore prevents the service from starting with known-invalid settings.

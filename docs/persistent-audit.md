# Persistent audit journal

The Gateway can persist security and control-plane events to an append-only JSONL journal.

## Safety properties

- `COMMANDER_AUDIT_PATH` defaults to `~/.remote-mcp-commander/audit.jsonl`.
- Newly created audit directories use mode `0700`; the audit file uses `0600` on POSIX.
- Existing parent directory permissions are never changed.
- POSIX `O_NOFOLLOW` rejects an audit path that is replaced with a symlink.
- Sensitive field names such as token, secret, credential, authorization, content, stdout, and stderr are recursively redacted before logging and persistence.
- Strings, collections, nesting depth, query result count, and query scan bytes are bounded.
- Mutation preflight events use fail-closed persistence. If the required audit append fails, the mutation is not dispatched to the Agent.

## Querying

The journal is intentionally not exposed as an MCP tool. Operators can query recent records through the authenticated Gateway control API:

```bash
curl -H "Authorization: Bearer $COMMANDER_CONTROL_TOKEN" \
  'http://127.0.0.1:8765/api/v1/audit?limit=100&agent_id=server-01'
```

Optional exact-match filters are `event` and `agent_id`. Results are newest first. The Gateway scans only the configured tail window (`COMMANDER_AUDIT_QUERY_MAX_SCAN_BYTES`) instead of loading an unbounded journal.

## Durability

`COMMANDER_AUDIT_FSYNC=false` is the default to avoid forcing a disk flush for every control-plane event. Set it to `true` when stronger per-record durability is required and the audit file is on suitable local storage. A dedicated local path is preferred over NFS because audit latency is part of fail-closed mutation preflight.

This MVP does not yet provide cryptographic hash chaining, remote log shipping, rotation, retention policy, or multi-user actor identity. Those are separate hardening stages.

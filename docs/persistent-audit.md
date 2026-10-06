# Persistent audit journal

The Gateway persists security and control-plane events to a redacted, SHA-256 chained JSONL journal. Local persistence remains the source of truth; optional remote shipping sends the same chained records to an external collector.

## Safety properties

- `COMMANDER_AUDIT_PATH` defaults to `~/.remote-mcp-commander/audit.jsonl`.
- Newly created audit directories use mode `0700`; audit files use `0600` on POSIX.
- Existing parent directory permissions are never changed.
- POSIX `O_NOFOLLOW` rejects active or retained audit files replaced with symlinks.
- Sensitive field names such as token, secret, credential, authorization, content, stdout, and stderr are recursively redacted before local or remote delivery.
- Strings, collections, nesting depth, query result count, query scan bytes, local file size, and retained file count are bounded.
- Mutation preflight events use fail-closed local persistence. If the required local append fails, the mutation is not dispatched.

## Hash chain

Each new record contains:

- `audit_chain_id`: a random ID retained across Gateway restarts and rotations;
- `audit_sequence`: a monotonically increasing integer;
- `audit_previous_hash`: the prior record hash, or 64 zeroes at genesis;
- `audit_hash`: SHA-256 over canonical JSON for the complete record except `audit_hash` itself.

The Gateway verifies every retained file before accepting new records at startup. Invalid JSON, partial metadata, hash mismatch, sequence gaps, chain-ID changes, or broken previous-hash links prevent Gateway startup. Existing unchained records from versions before 0.26.0 are preserved as a legacy prefix; newly appended records begin one chain after that prefix.

Verify a stopped Gateway journal offline:

```bash
remote-mcp-audit verify /var/lib/remote-mcp-commander/audit.jsonl \
  --retention-files 5 --json
```

While the Gateway is running, the authenticated control endpoint returns the same verification summary:

```bash
curl -H "Authorization: Bearer $COMMANDER_CONTROL_TOKEN" \
  http://127.0.0.1:8765/api/v1/audit/verify
```

The summary includes checked and legacy record counts, retained file count, sequence range, retained anchor hash, and current head hash. When retention deletes the oldest file, the first remaining record's previous hash becomes the retained anchor. Preserve remote copies of earlier records if end-to-end verification beyond the local retention window is required.

A plain hash chain is tamper-evident, not tamper-proof against an attacker who can rewrite every retained file and recompute the chain. Independent remote retention or externally recorded head hashes provide the necessary outside anchor.

## Rotation and retention

`COMMANDER_AUDIT_MAX_BYTES` defaults to 16 MiB. Before an append would exceed that size, the Gateway rotates:

- `audit.jsonl` is the active file;
- `audit.jsonl.1` is the newest rotated file;
- higher numeric suffixes are older.

`COMMANDER_AUDIT_RETENTION_FILES` defaults to 5 and counts the active file. The oldest file is deleted during rotation once the configured total would be exceeded. Rotation preserves the hash chain across file boundaries. With `COMMANDER_AUDIT_FSYNC=true`, record writes and rotation directory metadata are flushed.

Recent-record queries scan the active file and retained rotations from newest to oldest under one total `COMMANDER_AUDIT_QUERY_MAX_SCAN_BYTES` budget.

## Remote shipping

Set a fixed collector URL to synchronously POST each locally persisted, redacted, chained JSON record:

```env
COMMANDER_AUDIT_REMOTE_URL=https://audit.example.com/v1/events
COMMANDER_AUDIT_REMOTE_TOKEN=replace-with-dedicated-random-credential
COMMANDER_AUDIT_REMOTE_TIMEOUT_S=2
COMMANDER_AUDIT_REMOTE_REQUIRED=false
```

Non-loopback collectors must use HTTPS; loopback development collectors may use HTTP. Delivery disables environment proxy inheritance and redirects. If configured, the dedicated token is sent as a Bearer credential and must not equal any Gateway, approval-admin, MCP, or Agent credential.

Local append happens before remote delivery. With the default `REMOTE_REQUIRED=false`, delivery failure is logged and local operation continues. With `REMOTE_REQUIRED=true`, failure propagates only from fail-closed `audit_required` mutation preflight calls, so the associated mutation is not dispatched. Best-effort informational events remain locally recorded and do not stop control-plane work.

There is no durable retry queue in this stage. A collector outage can create remote gaps in optional mode; sequence numbers and previous hashes make those gaps visible. Required mode trades mutation availability for collector delivery at the request boundary.

## Querying

The journal is intentionally not exposed as an MCP tool. Operators can query recent records through the authenticated Gateway control API:

```bash
curl -H "Authorization: Bearer $COMMANDER_CONTROL_TOKEN" \
  'http://127.0.0.1:8765/api/v1/audit?limit=100&agent_id=server-01'
```

Optional exact-match filters are `event` and `agent_id`. Results are newest first and include chain metadata. The Gateway scans only the configured retained tail budget instead of loading unbounded history.

## Durability

`COMMANDER_AUDIT_FSYNC=false` avoids forcing a disk flush for every event. Set it to `true` when stronger per-record durability is required and the journal is on suitable local storage. A dedicated local path is preferred over NFS because audit latency is part of fail-closed mutation preflight.

Multi-user actor identity remains a later authorization stage.

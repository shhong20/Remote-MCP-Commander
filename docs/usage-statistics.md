# Usage statistics

Remote MCP Commander exposes `get_usage_stats` as a local, secret-free summary of the bounded Gateway audit window.

## Scope

The result is not a billing counter or monthly quota. It summarizes only the audit bytes scanned under the existing `audit_query_max_scan_bytes` bound. `scan_truncated=true` means older retained audit data was outside that bounded window.

An optional `agent_id` restricts the aggregation to one Agent. When omitted, the summary covers all records in the scanned local audit window.

## Returned metrics

- audit records scanned
- result-bearing records
- successful and failed result counts
- rejected and timed-out result counts
- success rate over result-bearing records only
- request/result latency sample count
- average, p50, and p95 latency for request IDs whose request and result are both inside the scan window
- earliest and latest timestamp in the scanned matching records
- up to 100 exact audit event names ordered by frequency

Requested-only audit records do not enter the success/failure denominator. A result is treated as failed when it is rejected, times out, has a non-zero integer return code, carries an error value, or uses a `*_failed`/`*_denied` terminal event.

## Privacy and authority

Usage aggregation happens inside the Gateway. Raw audit records are not returned by this tool. Paths, argv values, cwd values, environment values, request IDs, hashes, file contents, command output, and credentials are not part of the response model.

The endpoint uses the same control-token authentication as the existing audit query and does not add Agent capabilities or filesystem/command authority.

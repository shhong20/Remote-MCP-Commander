# Batch path mutations

`mutate_paths` groups existing filesystem mutation operations into one ordered MCP call without adding a new Agent or Gateway filesystem authority.

## Operations

Each item supports one existing mutation:

- `mkdir`: `path`, optional `parents`
- `copy`: `path` + `destination`, optional `overwrite`
- `move`: `path` + `destination`, optional `overwrite`
- `delete`: `path`

Irrelevant fields are rejected by the MCP input model before execution. For example, `delete` cannot carry `destination` or `overwrite`, and `copy`/`move` require `destination`.

## Ordering

The batch accepts at most 16 operations and executes them **sequentially in input order**. It intentionally does not use concurrent execution because path mutations can depend on previous items or overlap the same files/directories.

This allows chains such as:

1. create a directory
2. copy a file into it
3. move/rename that file
4. delete the renamed file

without races introduced by the batch layer.

## Failure behavior

`stop_on_error` defaults to `true`.

- when an Agent result is `rejected=true`, the batch stops before later operations
- Gateway HTTP errors, transport failures, and response-validation failures are returned as bounded item errors
- with `stop_on_error=false`, later independent operations continue after an item failure
- `completed_count` tells how many operations were attempted
- `stopped_early` indicates that unattempted items remain because of fail-fast behavior

## Safety

The batch calls `GatewayClient.mutate_path()` once per item. Therefore every operation still passes through the existing:

- `filesystem.mutate` capability check
- Gateway mutation route
- Agent allowed-root checks
- symlink safeguards
- root-path mutation protections
- non-recursive delete restriction
- regular-file-only copy rules
- overwrite restrictions
- per-operation audit records

No new Agent capability, filesystem route, or bypass path is introduced by `mutate_paths`.

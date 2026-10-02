# Operator config control

Remote MCP Commander exposes a deliberately narrow operator-tuning layer in Personal mode.
It is not a general environment editor and cannot change filesystem authority, credentials,
approval policy, command allowlists, network binding, OAuth, or audit destinations.

## Tools

- `get_operator_config()` reads the effective values in the running Gateway process, the
  persisted overrides, the values desired after restart, and `restart_required`.
- `set_operator_config(key, value)` persists one allowlisted numeric override.
- `set_operator_config(key, null)` removes that override and returns the setting to its
  environment/default value after restart.

Changes are persistent but are not live-reloaded. Restart Gateway, Agent, and MCP server to
apply them consistently. The response explicitly reports `restart_required=true` while the
running process differs from the desired persisted configuration.

## Mutable keys

- `max_output_bytes`
- `exec_timeout_s`
- `session_timeout_s`
- `session_input_max_bytes`
- `session_max_active`
- `session_history_limit`
- `pty_timeout_s`
- `pty_max_active`
- `pty_input_max_bytes`
- `file_max_bytes`
- `transfer_max_bytes`
- `transfer_session_ttl_s`
- `transfer_max_active`
- `transfer_request_timeout_s`

Every value is still bounded by the corresponding `Settings` field. Cross-field constraints,
such as `session_history_limit >= session_max_active`, are validated before persistence.

## Persistence and safety

The default file is `~/.remote-mcp-commander/operator-config.json` and can be relocated only
through trusted startup configuration (`COMMANDER_OPERATOR_CONFIG_PATH`). The file:

- must be a regular non-symlink file when it already exists;
- must be owned by the service user on POSIX;
- must not be group/world accessible;
- is written atomically through a mode-0600 temporary file and directory fsync;
- accepts only the fixed mutable-key allowlist and numeric values;
- causes startup to fail closed if malformed, insecure, or semantically invalid.

Gateway mutation is allowed only while `operation_mode=personal`. A required audit record is
written before persistence. Audit records contain only the setting key, whether it was reset,
and whether a restart is required; credentials or arbitrary configuration payloads are never
logged.

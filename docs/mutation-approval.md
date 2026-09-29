# Mutation Approval Invariants

Mutation approval is deliberately separate from MCP authentication and the normal Gateway control credential.

- Approval issuance is not exposed as an MCP tool.
- `COMMANDER_APPROVAL_ADMIN_TOKEN` must be unique from MCP, control, and Agent credentials.
- A grant binds exactly one Agent ID, operation, and target.
- Grant secrets are random and only SHA-256 hashes are stored by the Gateway.
- Grants are short-lived and single-use; concurrent replay allows only one successful consumer.
- Wrong secrets or binding mismatches do not destroy the valid grant.
- The Gateway consumes approval before dispatch, giving at-most-once mutation semantics.
- Process mutation binds PID plus process creation time to prevent PID-reuse confusion.
- Process termination does not escalate to hard kill.
- Service mutation accepts only `start`, `stop`, or `restart` on a validated unit name and never invokes a shell or sudo.
- PTY start binds the complete canonical argv hash and additionally requires separate Gateway and Agent PTY allowlists.
- PTY input and output content are not persisted in audit records; input mutations record byte counts only.
- Generic `execute` cannot be configured to run arbitrary interpreters or service/process mutation tools; only fixed safe command profiles are available.
- Pending approvals are memory-only in this MVP and are invalidated by a Gateway restart.

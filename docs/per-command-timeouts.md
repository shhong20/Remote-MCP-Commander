# Per-command timeout overrides

Remote command calls can override timeout budgets without changing Agent-wide defaults.

`execute(..., timeout_s=...)` accepts 0.1 to 60 seconds. One-shot calls intentionally remain short-lived because the Gateway/MCP HTTP request stays open until completion. Gateway and MCP client request deadlines are automatically extended beyond the requested command timeout so transport timeouts do not fire first.

`start_command(..., timeout_s=...)` accepts 1 to 3600 seconds. The start call still returns immediately; the Agent stores the effective timeout on the session and uses it for process supervision. Session status and `list_sessions` expose the applied timeout.

When omitted, `COMMANDER_EXEC_TIMEOUT_S` and `COMMANDER_SESSION_TIMEOUT_S` remain the defaults. New Agents advertise `command.timeout`, and the Gateway requires that capability whenever an override is supplied so rolling upgrades fail closed rather than silently ignoring the field.

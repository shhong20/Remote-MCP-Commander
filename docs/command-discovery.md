# Command discovery

`list_commands` reports which structured command profiles are currently resolvable on one Agent.

The response contains only executable names, not absolute executable paths or the Agent PATH:

- `operation_mode`: `hardened` or `personal`
- `generic_available`: generic command profiles that currently resolve
- `generic_unavailable`: allowed profiles that are not currently resolvable
- `pty_available`: PTY executable profiles that currently resolve
- `pty_unavailable`: PTY profiles that are configured but not resolvable

Discovery does not change any allowlist or permission. Execution still passes through the existing Gateway and Agent policy checks. Older Agents that do not advertise `command.discovery` fail closed at the Gateway.

In Personal mode this lets the client choose among the actual installed tools instead of repeatedly probing names such as `rg`, `pytest`, `docker`, or `node` and learning availability from command failures.

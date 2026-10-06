# Read-only diagnostics

These tools cover common server checks without opening a shell or widening the generic command profile.

## MCP tools

- `system_health(agent_id)` returns bounded host health metrics.
- `lookup_port(agent_id, port)` returns listeners for one TCP port.
- `service_logs(agent_id, unit, lines=100)` returns recent bounded journal entries.
- `git_status(agent_id, path)` returns bounded Git porcelain-v2 status.

All four tools travel over the existing authenticated Gateway/Agent channel and remain read-only.

## System health

Health collection uses `psutil` in a worker thread. The result includes uptime, logical CPU count, short CPU utilization, load averages where available, memory, swap, and root-filesystem usage.

No process command lines, environments, file contents, or network payloads are returned.

## TCP port lookup

Port lookup accepts exactly one port in `1..65535`. It inspects TCP listeners only and returns at most 50 records containing the local address, PID when visible, and process name when visible.

If the Agent OS user cannot inspect the system connection table, the operation fails closed instead of elevating privileges.

## systemd logs

Service log reads validate the unit name using the same conservative service-name contract as service status/actions. The Agent invokes a fixed trusted-path `journalctl` command with `--no-pager`, a fixed output format, a bounded line count (`1..500`), and the existing output-byte cap.

The Agent never invokes `sudo`. Access therefore cannot exceed the Agent OS user's journal permissions. Log content is returned only to the caller and is not copied into the persistent audit journal.

## Git status

Git status requires the requested directory and repository metadata to remain inside `COMMANDER_ALLOWED_ROOTS_JSON`.

The initial implementation supports only ordinary `.git/` directories. It rejects `.git` symlinks and gitfile/worktree repositories because their metadata may point outside an allowed root.

The fixed Git invocation disables fsmonitor hooks, repository hooks, system/global config, pager execution, terminal prompts, and submodule traversal. Output is `--porcelain=v2 --branch`, bounded by the configured output cap. If output is truncated, `clean` is returned as unknown rather than incorrectly claiming a clean or dirty repository.

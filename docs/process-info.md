# Direct process metadata lookup

`process_info(agent_id, pid)` reads metadata for exactly one operating-system process without scanning the whole process table.

Returned metadata is intentionally limited to PID, creation time in milliseconds, process name, username, status, and RSS memory. Command-line arguments, environment variables, open files, and process memory are never exposed.

This complements bounded `list_processes`: busy servers may have more than the list limit, so a recently created high-PID command or PTY session can be missing from that list. The session's `pid` can now be looked up directly and its `create_time_ms` compared before using guarded signal or termination flows.

If the process no longer exists, the request fails closed. If creation-time identity itself cannot be read, the result is rejected rather than returning an unverifiable PID. Less critical metadata such as username, status, or RSS may be null when access is denied.

New Agents advertise `process.info`. The Gateway requires the capability before dispatch so older Agents fail closed with HTTP 409 instead of silently ignoring the request.

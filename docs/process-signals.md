# Structured process signals

`signal_process(agent_id, pid, expected_create_time_ms, signal, ...)` sends one of `term`, `kill`, `int`, or `hup` through a structured Agent path.

The Agent re-opens the PID with psutil and verifies the exact millisecond creation time immediately before sending the signal. A reused PID is rejected. The Agent also refuses to signal its own process and relies on normal OS permissions for all other targets.

In Hardened mode, a one-use approval is mandatory and is bound to both the process identity and exact signal operation (`process.signal.term`, `process.signal.kill`, `process.signal.int`, or `process.signal.hup`).

In Personal mode, approval is optional by default. Set `COMMANDER_PERSONAL_PROCESS_APPROVAL_REQUIRED=true` to require approval there as well. Supplying only one approval field is rejected; supplying both consumes the approval normally.

The legacy `terminate_process` endpoint remains unchanged and always requires its original `process.terminate` approval.

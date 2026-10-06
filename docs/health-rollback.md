# Automatic health-check rollback

`remote-mcp-release activate-checked` performs a bounded native-stack rollout and automatically
restores the previous trusted release when the candidate does not become healthy.

This command is intentionally narrower than a general deployment hook. It accepts no executable,
shell command, service unit, or health URL from the caller.

## Fixed rollout contract

The command uses only:

- `/usr/bin/systemctl`
- `remote-mcp-gateway.service`
- `remote-mcp-server.service`
- `http://127.0.0.1:8765/healthz`
- `/opt/remote-mcp-commander` as the exact deployment root

The subprocess environment is reduced to a fixed PATH and C locale. It never invokes a shell or
`sudo`. The caller must already have OS permission to restart the two units.

Health requires both systemd units to be active and the Gateway endpoint to return HTTP 200 with
the exact JSON object `{"status":"ok"}`. HTTP proxy environment variables are ignored and
redirects are rejected. This confirms native service state plus Gateway readiness; it is not an
end-to-end MCP tool or remote-Agent test.

## Activation sequence

An existing, different `current` release is mandatory because it is the rollback target. Both the
candidate and current release are verified against the release trust store before links change.

```bash
remote-mcp-release \
  --root /opt/remote-mcp-commander \
  --json \
  activate-checked 2026.09.29-c \
  --timeout-seconds 30 \
  --interval-seconds 0.5
```

The sequence is:

1. acquire the release lock
2. verify the candidate and current release signatures and exact trees
3. switch `previous` to the old current release and `current` to the candidate
4. restart the fixed native stack
5. poll bounded service and Gateway health
6. return success only when the candidate becomes healthy

The timeout is limited to 1–300 seconds. The interval is limited to 0.1–10 seconds and cannot
exceed the timeout.

## Failure and recovery

If candidate restart or health fails, the command:

1. re-verifies the previous release
2. restores it as `current`
3. records the failed candidate as `previous`
4. restarts the same fixed stack
5. polls recovery health with the same bounds
6. exits non-zero even when recovery succeeds

JSON failures include `rolled_back` and `recovery_healthy`. Operators must alert on the non-zero
exit and retain the failed candidate for diagnosis. If `rolled_back=true` but
`recovery_healthy=false`, the trusted link was restored but the stack still requires immediate
operator intervention.

The release lock remains held through activation, probing, and recovery so another activation or
rollback cannot interleave with the rollout.

## Bootstrap and other topologies

Use ordinary `activate` for the first deployment because no trusted rollback target exists.
Ordinary `activate` still changes links only and never restarts services.

Custom units, remote health URLs, user-service Agents, rolling multi-host deployments, and
application-specific smoke tests remain external orchestration concerns. They are deliberately not
converted into arbitrary command hooks in the release manager.

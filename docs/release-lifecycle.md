# Release lifecycle

Production services reference a stable `current` path while release contents live under `releases/<release-id>`.

```text
/opt/remote-mcp-commander/
  releases/
    2026.09.29-a/
    2026.09.29-b/
  current  -> releases/2026.09.29-b
  previous -> releases/2026.09.29-a
```

Prepare release directories outside the release manager. Each candidate must be a real direct child directory and contain an executable `.venv/bin/remote-mcp-doctor`.

The release CLI never downloads code, installs packages, restarts services, or executes arbitrary release scripts.

## Commands

```bash
remote-mcp-release --root /opt/remote-mcp-commander status
remote-mcp-release --root /opt/remote-mcp-commander activate 2026.09.29-b
remote-mcp-release --root /opt/remote-mcp-commander rollback
```

`--json` is available for automation. Release IDs are limited to a conservative filename-safe format.

## Safety properties

- deployment root and `releases/` must be real directories, not symlinks
- candidate releases cannot be symlink aliases or escape `releases/`
- `current` and `previous` must be relative `releases/<id>` links
- a lock file serializes concurrent activate/rollback operations on POSIX
- each individual symlink switch uses temporary-link + `os.replace` atomic replacement
- activation is idempotent if the requested release is already current

The two links are not a filesystem transaction as a pair. The order is chosen so a failed activation does not switch `current`; a rollback switches `current` first so service recovery takes priority over refreshing `previous`.

## Operational sequence

After `activate`, restart the intended service normally. Its existing systemd `ExecStartPre=remote-mcp-doctor <role>` validates the newly selected release environment before the process starts.

If startup or smoke checks fail:

```bash
remote-mcp-release --root /opt/remote-mcp-commander rollback
systemctl restart remote-mcp-gateway remote-mcp-server
```

Use the same layout under the Agent user's application directory for user-service deployments. Release switching does not alter registry, audit, enrollment token, or environment files, which remain outside the release directory.

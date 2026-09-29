# Release lifecycle

Production services reference a stable `current` path while immutable release contents live under `releases/<release-id>`.

```text
/opt/remote-mcp-commander/
  releases/
    2026.09.29-a/
    2026.09.29-b/
  current  -> releases/2026.09.29-b
  previous -> releases/2026.09.29-a
```

Prepare release directories outside the release manager. Each candidate must be a real direct child directory, contain an executable `.venv/bin/remote-mcp-doctor`, and contain no `.git` metadata. Runtime state, credentials, registry data, and audit data stay outside release directories.

The release CLI never downloads code, installs packages, restarts services, or executes arbitrary release scripts.

## Commands

```bash
remote-mcp-release --root /opt/remote-mcp-commander status
remote-mcp-release --root /opt/remote-mcp-commander \
  seal 2026.09.29-b --commit-sha <exact-git-commit>
remote-mcp-release --root /opt/remote-mcp-commander verify 2026.09.29-b
remote-mcp-release --root /opt/remote-mcp-commander activate 2026.09.29-b
remote-mcp-release --root /opt/remote-mcp-commander rollback
```

`--json` is available for automation. `status` distinguishes structurally available releases from sealed releases. Release IDs are limited to a conservative filename-safe format.

## Safety properties

- deployment root and `releases/` must be real directories, not symlinks
- candidate releases cannot be symlink aliases or escape `releases/`
- activation and rollback both require a valid exact-tree release manifest
- changed, missing, or extra files prevent activation
- `current` and `previous` must be relative `releases/<id>` links
- a lock file serializes concurrent activate/rollback operations on POSIX
- each individual symlink switch uses temporary-link + `os.replace` atomic replacement
- activation is idempotent if the requested release is already current

The two links are not a filesystem transaction as a pair. Activation verifies the target before updating links. Rollback verifies `previous` before switching `current` back.

## Operational sequence

Seal and verify the candidate, then activate it. After `activate`, restart the intended service normally. Its existing systemd `ExecStartPre=remote-mcp-doctor <role>` validates the selected release environment before the process starts.

If startup or smoke checks fail:

```bash
remote-mcp-release --root /opt/remote-mcp-commander verify <previous-id>
remote-mcp-release --root /opt/remote-mcp-commander rollback
systemctl restart remote-mcp-gateway remote-mcp-server
```

Before the first upgrade from a legacy unsealed deployment, quiesce and seal the existing current release so it can be used as a verified rollback target. The systemd templates set `PYTHONDONTWRITEBYTECODE=1` so normal Python startup does not create new `__pycache__` content inside a sealed release.

Use the same layout under the Agent user's application directory for user-service deployments. See `release-integrity.md` for the manifest format and trust boundary.

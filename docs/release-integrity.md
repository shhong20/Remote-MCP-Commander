# Release integrity

Every production release must be sealed before `activate` can select it. Sealing creates `release-manifest.json` inside the candidate release and immediately verifies the resulting manifest against the tree.

The manifest records:

- release ID
- package version
- source commit SHA supplied by the release builder
- supported Agent/Gateway protocol range
- UTC creation time
- total regular-file bytes
- a sorted exact inventory of directories, regular files, and symlinks
- SHA-256 for every regular file
- file mode and symlink target metadata
- an aggregate SHA-256 over the canonical entry list

The manifest itself is excluded from its own tree hash and is written read-only (`0444`) on POSIX.
## Verification boundary

`verify` rescans the complete release tree and rejects any changed, missing, or newly added entry. File-content changes, executable-bit changes, symlink-target changes, and metadata/source-version mismatches therefore prevent activation.

Release artifacts may not contain `.git` metadata, special files, or regular-file hard links. General symlinks must resolve inside the release. The only external symlink exception is `.venv/bin/python*`, because Python virtual environments commonly point those interpreter names at the host Python installation.

The release scan is bounded to 50,000 entries, 8 GiB of regular-file content, and a 16 MiB manifest. Duplicate JSON keys and writable manifest files are rejected.

This boundary protects the release artifact itself. The host Python binary reached through the virtual-environment interpreter symlink remains a host dependency and is outside the release manifest.

## Authenticity layer

The manifest by itself provides integrity, not cryptographic provenance. Production activation therefore adds the Ed25519 signature layer documented in `release-signing.md`. A valid tree manifest without a trusted signature remains untrusted and cannot be activated.

## Commands

Prepare an immutable release directory first, without `.git` metadata. Then seal and verify it before activation:

```bash
remote-mcp-release --root /opt/remote-mcp-commander \
  seal 2026.09.29-b --commit-sha <exact-git-commit>

remote-mcp-release --root /opt/remote-mcp-commander \
  verify-integrity 2026.09.29-b

remote-mcp-release --root /opt/remote-mcp-commander \
  sign 2026.09.29-b --key-id ops-2026 --private-key /secure/release-signing-private.pem

remote-mcp-release --root /opt/remote-mcp-commander verify 2026.09.29-b
remote-mcp-release --root /opt/remote-mcp-commander activate 2026.09.29-b
```

`activate` performs verification again while holding the release-manager lock. `rollback` likewise verifies `previous` before switching `current` back.

For the first upgrade from an older unsealed deployment, quiesce the old service and seal the existing current release before activating the first manifested release. Otherwise rollback to that legacy release will intentionally fail closed.
# Signed package publication

Reproducible package bundles become trusted publication artifacts only after the exact `package-manifest.json` bytes are signed with a dedicated Ed25519 package-signing key.

Package publication and deployed release activation are separate trust domains:

```text
Git commit
  -> reproducible package bundle
  -> package-manifest.json
  -> package-manifest.sig.json        trusted-package-keys/
  -> transport / artifact storage
  -> host prepares runtime release
  -> release-manifest.json
  -> release-manifest.sig.json        trusted-release-keys/
  -> activation
```

Use separate key pairs for package publication and release activation. Never copy a private signing key into a package bundle or release tree.

## Domain separation

The signed payload is:

```text
"remote-mcp-commander/package-manifest/v1\0" || exact package-manifest.json bytes
```

The domain prefix prevents an Ed25519 signature created for package publication from being reused as a release-manifest signature even if the same raw key were accidentally configured in both systems.

The signature metadata records the domain, key ID, manifest SHA-256, algorithm, and base64 Ed25519 signature. The signature file is written atomically and set read-only on POSIX.

## Sign and verify

```bash
remote-mcp-package sign \
  --bundle /srv/rmc-publication/0.20.0 \
  --key-id package-2026 \
  --private-key /secure/package-signing-private.pem
```

```bash
remote-mcp-package verify-trusted \
  --bundle /srv/rmc-publication/0.20.0 \
  --trusted-keys-dir /etc/remote-mcp-commander/trusted-package-keys
```

`verify` checks the package-manifest/artifact integrity contract and may be used before signing. `verify-trusted` additionally requires a valid package signature and trusted public key.

Private keys must be owner-only regular files outside the bundle. Trusted key directories and public-key PEMs must be non-symlink paths outside the bundle and must not be group/world writable.

## Rotation and revocation

Add a new package public key before publishing artifacts with the new key ID. Retain old public keys while any artifact that may still be consumed depends on them.

Removing `<key-id>.pem` immediately revokes package bundles signed only by that key. This affects package publication trust only; release activation trust is independently controlled by `trusted-release-keys/`.

The package signing layer authenticates artifact publication. It does not make the runtime virtual environment reproducible; dependency/runtime locking remains a separate step.

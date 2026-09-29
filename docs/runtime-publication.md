# Signed runtime-lock publication

A runtime bundle becomes an independently trusted deployment input only after the exact
`runtime-lock.json` bytes are signed with a dedicated Ed25519 runtime-signing key.

Package, runtime, and release publication are separate trust domains:

```text
signed package bundle          trusted-package-keys/
  -> exact wheel resolution
  -> runtime-lock.json
  -> runtime-lock.sig.json     trusted-runtime-keys/
  -> offline closure verification
  -> installed release tree
  -> release-manifest.json
  -> release-manifest.sig.json trusted-release-keys/
  -> activation
```

Use separate key pairs and trust directories for all three domains. Never place a private key
inside a package bundle, runtime bundle, or release tree.

## Domain separation

The signed payload is:

```text
"remote-mcp-commander/runtime-lock/v1\0" || exact runtime-lock.json bytes
```

This prefix prevents a runtime-lock signature from being reused as a package or release
signature even if an operator accidentally configures the same raw key in multiple trust stores.
The signature metadata records the domain, key ID, lock SHA-256, algorithm, and base64 Ed25519
signature. The signature file is written atomically and made read-only on POSIX.

## Sign and verify

Create the unsigned runtime bundle from an already trusted package, then sign it:

```bash
remote-mcp-runtime sign \
  --runtime-bundle /srv/rmc/runtime-0.22.0 \
  --package-bundle /srv/rmc/package-0.21.0 \
  --trusted-package-keys /etc/remote-mcp-commander/trusted-package-keys \
  --key-id runtime-2026 \
  --private-key /secure/runtime-signing-private.pem
```

A deployment host accepts the bundle with both upstream package trust and runtime publication
trust:

```bash
remote-mcp-runtime verify-trusted \
  --runtime-bundle /srv/rmc/runtime-0.22.0 \
  --package-bundle /srv/rmc/package-0.21.0 \
  --trusted-package-keys /etc/remote-mcp-commander/trusted-package-keys \
  --trusted-runtime-keys /etc/remote-mcp-commander/trusted-runtime-keys
```

Trusted verification authenticates the exact lock first, then performs the normal runtime checks:
package-signature verification, target matching, exact wheelhouse hashes and metadata, and fully
offline dependency-closure resolution. The plain `verify` command remains useful for integrity
checks but does not establish runtime publication authenticity.

Private keys must be owner-only regular files outside the runtime bundle. Trusted runtime key
directories and public-key PEMs must be non-symlink paths outside the bundle and must not be
group/world writable.

## Rotation and revocation

Install a new runtime public key before signing bundles with its key ID. Retain old keys while any
runtime bundle that may still be deployed depends on them.

Removing `<key-id>.pem` immediately revokes runtime bundles signed only by that key. It does not
revoke the upstream package or an already sealed release; those are controlled by their separate
trust stores and lifecycle policies.

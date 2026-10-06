# Signed release manifests

Release integrity manifests become trusted deployment artifacts only after an Ed25519 signature is verified against an operator-managed public key outside the release tree.

The default trust directory is:

```text
<deployment-root>/trusted-release-keys/
  ops-2026.pem
  ops-2027.pem
```

The signature stores only the signing key ID, SHA-256 of the exact manifest bytes, and the Ed25519 signature. `activate` and `rollback` require both exact-tree integrity and a trusted signature.

The trusted-key directory must be a real non-symlink directory and must not be group/world writable. Trusted public-key PEM files must be regular non-symlink files and must not be group/world writable.
## Generate and distribute a signing key

Generate the private key in a protected build/signing environment, not inside a release directory:

```bash
openssl genpkey -algorithm Ed25519 -out release-signing-private.pem
chmod 600 release-signing-private.pem
openssl pkey -in release-signing-private.pem -pubout -out ops-2026.pem
```

Copy only `ops-2026.pem` to the production trust directory. The private key should not be copied to the Gateway, MCP, or Agent release tree.

The signing CLI also rejects a private-key path that resolves inside the release directory. An unencrypted PEM is expected; CI systems can materialize a short-lived mode-0600 key file from their secret store for the signing step.
## Sign and verify

```bash
remote-mcp-release --root /opt/remote-mcp-commander \
  seal 2026.09.29-b --commit-sha <exact-git-commit>

remote-mcp-release --root /opt/remote-mcp-commander \
  sign 2026.09.29-b --key-id ops-2026 \
  --private-key /secure/signing/release-signing-private.pem

remote-mcp-release --root /opt/remote-mcp-commander verify 2026.09.29-b
remote-mcp-release --root /opt/remote-mcp-commander activate 2026.09.29-b
```

`verify-integrity` checks only the exact-tree manifest and is useful during artifact preparation. `verify` is the production trust check and additionally requires a trusted Ed25519 signature.
## Rotation and revocation

Key rotation is additive: install the new public key first, then sign new releases with its new key ID. Keep the old public key while any `current` or `previous` rollback target still depends on it.

Deleting a public-key PEM from the trusted directory immediately revokes trust for releases signed only by that key. Do not remove an old key until those rollback targets have been retired or re-signed according to your release policy.

The trust directory is deliberately outside `releases/<id>`. A release must never be allowed to provide its own trust anchor, because that would let an attacker replace both artifact and public key.
# Immutable artifact registry

`remote-mcp-artifact` publishes one trusted package bundle and its matching trusted runtime
bundle as a single immutable, content-addressed artifact set.

This first registry backend is a local filesystem contract. It is deliberately independent of a
cloud vendor: an object-storage or package-service adapter can transfer complete object
directories later without changing artifact identity or trust verification.

## Layout and identity

```text
<registry>/
  .registry.lock
  objects/
    <artifact-id>/
      publication.json
      package/
      runtime/
```

The artifact ID is SHA-256 over a domain-separated tuple containing the signed package-manifest
digest and signed runtime-lock digest. It does not depend on a mutable version alias, upload time,
or filesystem path. `publication.json` records both signing key IDs, the package commit/tree,
runtime target, version, and wheel count.

Every published object tree is read-only. Publication copies regular files into a private
temporary directory, re-verifies both signatures and the offline dependency closure, freezes the
tree, and atomically renames it into `objects/<artifact-id>`. Publishing the same trusted pair
again is idempotent; an existing object is fully re-verified and is never overwritten.

## Initialize and publish

The registry root must be absolute, real, and not group/world writable. Its parent must already
exist.

```bash
remote-mcp-artifact init \
  --root /srv/remote-mcp-artifacts

remote-mcp-artifact publish \
  --root /srv/remote-mcp-artifacts \
  --package-bundle /srv/rmc/package-0.21.0 \
  --runtime-bundle /srv/rmc/runtime-0.22.0 \
  --trusted-package-keys /etc/remote-mcp-commander/trusted-package-keys \
  --trusted-runtime-keys /etc/remote-mcp-commander/trusted-runtime-keys \
  --json
```

The result includes the artifact ID and exact registry paths for the package and runtime bundles.

## Verify by pinned identity

Consumers select an exact artifact ID rather than a mutable `latest` or version label:

```bash
remote-mcp-artifact verify \
  --root /srv/remote-mcp-artifacts \
  --artifact-id <64-character-sha256> \
  --trusted-package-keys /etc/remote-mcp-commander/trusted-package-keys \
  --trusted-runtime-keys /etc/remote-mcp-commander/trusted-runtime-keys
```

Verification requires:

- the requested ID to match the package/runtime signature digests
- the publication record to match the trusted manifests exactly
- valid package and runtime publication signatures
- exact package artifacts and runtime wheel hashes
- a matching runtime target and fully offline dependency closure
- a read-only artifact tree with no symlinks or special files

Package and runtime trust directories must be outside the registry. This prevents a party that can
replace registry data from replacing both an artifact and the public keys used to trust it.

## Transport and retention

Copy only complete `objects/<artifact-id>` directories between registry backends, preserve their
read-only shape, and run `verify` at the destination before use. A partial temporary directory is
not an artifact.

This layer intentionally has no mutable channels, tags, deletion command, or garbage collection.
Those require a separate authorization and retention policy. Version labels are metadata only;
deployment automation should pin the artifact ID.

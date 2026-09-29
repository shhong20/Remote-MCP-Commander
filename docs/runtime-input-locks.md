# Locked runtime wheel inputs

A reproducible application wheel is not enough to make a runtime reproducible. Dependency ranges still need to be resolved into an exact wheel set for a concrete Python/platform target.

`remote-mcp-runtime` creates a runtime bundle from an already trusted signed package bundle:

```text
signed package bundle
  -> pip wheel-only dependency resolution
  -> wheelhouse/
  -> runtime-lock.json
  -> offline pip closure verification
```

The first supported runtime target is intentionally narrow: CPython 3.11, Linux x86_64, with `pip==26.2.1` as the resolver contract.

## Create and verify

```bash
remote-mcp-runtime lock \
  --package-bundle /srv/rmc/package-0.20.0 \
  --trusted-package-keys /etc/remote-mcp-commander/trusted-package-keys \
  --output /srv/rmc/runtime-0.21.0

remote-mcp-runtime verify \
  --runtime-bundle /srv/rmc/runtime-0.21.0 \
  --package-bundle /srv/rmc/package-0.20.0 \
  --trusted-package-keys /etc/remote-mcp-commander/trusted-package-keys
```

Lock creation verifies the package signature first, downloads wheel artifacts only, and records the package-manifest SHA-256 and signing key ID so the runtime closure cannot silently be rebound to another package.

## Lock contents

`runtime-lock.json` records:

- trusted package-manifest SHA-256, package signing key ID, commit/tree SHA, and package version
- Python implementation/version, OS, machine, platform tag, and exact pip resolver version
- every wheel's project name, normalized name, version, filename, SHA-256, byte size, and declared wheel tags
- the fact that resolution is wheel-only

The wheelhouse is exact-set checked and must not be group/world writable. The lock itself is written read-only on POSIX.

Verification performs a fully offline `pip install --dry-run --ignore-installed --no-index --only-binary=:all: --find-links ... --report ...` and requires the resolved project/version set to match the lock exactly. This catches missing, unnecessary, incompatible, or dependency-incomplete wheelhouses.

## Trust boundary

This stage locks and validates dependency inputs; `runtime-lock.json` is not yet a publication authenticity anchor. A party able to replace both the lock and every dependency wheel could create a different but internally consistent closure.

The next trust layer should sign the exact runtime lock with a dedicated domain-separated publication signature before a host accepts the wheelhouse. Until that layer is present, treat runtime bundles as build-pipeline outputs rather than independently trusted deployment artifacts.

The lock also does not claim that the final virtual-environment directory is byte-for-byte reproducible. Installation paths, generated scripts, bytecode policy, and platform/runtime details remain separate from the exact input-wheel guarantee.

# Reproducible release packaging

`remote-mcp-package` creates transport artifacts from a committed Git object, not from the mutable working tree. A build therefore cannot accidentally include local edits, untracked files, `.env`, caches, or credentials.

## Build contract

The packaging contract is intentionally narrow:

- Python builder minor: `3.11`
- `build==1.6.1`
- `setuptools==84.0.0`
- `wheel==0.48.0`
- backend: `setuptools.build_meta`
- build-system requirement: exactly `setuptools==84.0.0`
- no `setup.py`, `setup.cfg`, or dynamic setuptools metadata
- `SOURCE_DATE_EPOCH` is the Git commit timestamp
- `PYTHONHASHSEED=0`, UTC timezone, bytecode disabled
- wheel build uses `--no-isolation` and `PIP_NO_INDEX=1`

Install the packaging toolchain with `pip install -e '.[packaging]'` before building.

## Build

```bash
remote-mcp-package build \
  --repo /path/to/Remote-MCP-Commander \
  --commit <exact-commit-or-ref> \
  --output /tmp/rmc-package

remote-mcp-package verify --bundle /tmp/rmc-package
```

The builder resolves the requested ref to an exact commit and Git tree, reads tracked blobs directly from Git, and generates a normalized uncompressed USTAR source archive. The source archive is built twice and both hashes must match.

Two independent source directories are then materialized from the same Git blobs. The wheel is built once from each tree. Filename, byte length, and SHA-256 must match before the bundle is emitted.

## Package manifest

`package-manifest.json` records:

- exact commit SHA and Git tree SHA
- commit timestamp used as `SOURCE_DATE_EPOCH`
- package version and Agent protocol range
- builder Python minor and exact toolchain versions
- source archive filename, size, and SHA-256
- wheel filename, size, and SHA-256
- `reproducibility_verified=true`

`verify` requires the bundle file set to match the manifest exactly and rechecks artifact hashes and normalized source-tar metadata.

## Trust boundary

This proves deterministic packaging under the declared build contract; it is not yet an artifact-publication trust anchor. `package-manifest.json` is currently unsigned. The deployed release still receives the existing exact-tree release manifest and trusted Ed25519 signature before activation.

The runtime virtual environment is deliberately not called reproducible. Dependency resolution, platform wheels, native libraries, and absolute venv paths need a separate locked runtime-input contract. Do not archive an arbitrary `.venv` and treat it as the reproducible artifact.

A follow-up publication layer can sign the package manifest and bind dependency-lock/runtime-input metadata before artifacts are distributed to hosts.

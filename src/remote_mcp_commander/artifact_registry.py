from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from remote_mcp_commander.package import PackagingError, _read_small_regular
from remote_mcp_commander.package_signing import (
    PackageSigningError,
    verify_signed_package_bundle,
)
from remote_mcp_commander.runtime_lock import RuntimeLockError, RuntimeTarget
from remote_mcp_commander.runtime_signing import (
    RuntimeSigningError,
    verify_signed_runtime_bundle,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

REGISTRY_SCHEMA_VERSION = 1
PUBLICATION_FILENAME = "publication.json"
OBJECTS_DIRNAME = "objects"
PACKAGE_DIRNAME = "package"
RUNTIME_DIRNAME = "runtime"
PUBLICATION_DOMAIN = b"remote-mcp-commander/artifact-publication/v1\x00"
SHA256_PATTERN = r"^[a-f0-9]{64}$"
SHA256_RE = re.compile(SHA256_PATTERN)
MAX_PUBLICATION_BYTES = 64 * 1024
MAX_TREE_ENTRIES = 20_000


class ArtifactRegistryError(RuntimeError):
    pass


def artifact_id_for(package_manifest_sha256: str, runtime_lock_sha256: str) -> str:
    if not SHA256_RE.fullmatch(package_manifest_sha256) or not SHA256_RE.fullmatch(
        runtime_lock_sha256
    ):
        raise ArtifactRegistryError("artifact identity requires canonical SHA-256 values")
    payload = (
        PUBLICATION_DOMAIN
        + package_manifest_sha256.encode("ascii")
        + b"\x00"
        + runtime_lock_sha256.encode("ascii")
    )
    return hashlib.sha256(payload).hexdigest()


class ArtifactPublication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = REGISTRY_SCHEMA_VERSION
    artifact_id: str = Field(pattern=SHA256_PATTERN)
    package_version: str = Field(min_length=1, max_length=128)
    package_manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    package_signing_key_id: str = Field(min_length=1, max_length=64)
    runtime_lock_sha256: str = Field(pattern=SHA256_PATTERN)
    runtime_signing_key_id: str = Field(min_length=1, max_length=64)
    package_commit_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    package_git_tree_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    target: RuntimeTarget
    wheel_count: int = Field(ge=1, le=256)

    @model_validator(mode="after")
    def validate_artifact_id(self) -> ArtifactPublication:
        expected = artifact_id_for(self.package_manifest_sha256, self.runtime_lock_sha256)
        if self.artifact_id != expected:
            raise ValueError("artifact ID does not match package/runtime digests")
        return self


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactRegistryError(f"duplicate publication JSON key: {key}")
        result[key] = value
    return result


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _validate_registry_path(root: Path) -> Path:
    root = root.expanduser()
    if not root.is_absolute():
        raise ArtifactRegistryError("registry root must be an absolute path")
    if root.is_symlink():
        raise ArtifactRegistryError("registry root must not be a symlink")
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise ArtifactRegistryError("registry root does not exist") from exc
    if not root.is_dir():
        raise ArtifactRegistryError("registry root must be a directory")
    if os.name != "nt" and stat.S_IMODE(root.stat().st_mode) & 0o022:
        raise ArtifactRegistryError("registry root must not be group/world writable")
    objects = root / OBJECTS_DIRNAME
    if objects.is_symlink() or not objects.is_dir():
        raise ArtifactRegistryError("registry objects directory is missing or unsafe")
    if os.name != "nt" and stat.S_IMODE(objects.stat().st_mode) & 0o022:
        raise ArtifactRegistryError("registry objects directory must not be group/world writable")
    return root


def _require_external_trust(root: Path, trust_path: Path, *, label: str) -> None:
    trust_input = trust_path.expanduser()
    if trust_input.is_symlink():
        raise ArtifactRegistryError(f"{label} must not be a symlink")
    try:
        resolved = trust_input.resolve(strict=True)
    except OSError as exc:
        raise ArtifactRegistryError(f"{label} is missing or unsafe") from exc
    if resolved.is_relative_to(root):
        raise ArtifactRegistryError(f"{label} must be outside the registry")


def init_registry(root: Path) -> Path:
    root = root.expanduser()
    if not root.is_absolute():
        raise ArtifactRegistryError("registry root must be an absolute path")
    if root.exists() or root.is_symlink():
        return _validate_registry_path(root)
    try:
        parent = root.parent.resolve(strict=True)
    except OSError as exc:
        raise ArtifactRegistryError("registry parent does not exist") from exc
    if not parent.is_dir():
        raise ArtifactRegistryError("registry parent must be a directory")
    try:
        root.mkdir(mode=0o755)
        (root / OBJECTS_DIRNAME).mkdir(mode=0o755)
    except OSError:
        raise
    return _validate_registry_path(root)


@contextmanager
def _registry_lock(root: Path) -> Iterator[None]:
    path = root / ".registry.lock"
    if path.is_symlink():
        raise ArtifactRegistryError("registry lock must not be a symlink")
    flags = os.O_CREAT | os.O_RDWR | int(getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ArtifactRegistryError("cannot open registry lock safely") from exc
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write_publication(path: Path, publication: ArtifactPublication) -> None:
    data = _canonical_json_bytes(publication.model_dump(mode="json"))
    if len(data) > MAX_PUBLICATION_BYTES:
        raise ArtifactRegistryError("publication record exceeds the size limit")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    if os.name != "nt":
        os.chmod(path, 0o444)


def load_publication(artifact_dir: Path) -> ArtifactPublication:
    path = artifact_dir / PUBLICATION_FILENAME
    if path.is_symlink():
        raise ArtifactRegistryError("publication record must not be a symlink")
    if os.name != "nt":
        try:
            if stat.S_IMODE(path.stat().st_mode) & 0o222:
                raise ArtifactRegistryError("publication record must be read-only")
        except OSError as exc:
            raise ArtifactRegistryError("publication record is missing or unsafe") from exc
    try:
        data = _read_small_regular(
            path,
            limit=MAX_PUBLICATION_BYTES,
            label="artifact publication",
        )
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        return ArtifactPublication.model_validate(payload)
    except ArtifactRegistryError:
        raise
    except Exception as exc:
        raise ArtifactRegistryError("publication record is invalid") from exc


def _copy_file(source: Path, destination: Path) -> None:
    source_flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0))
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_NOFOLLOW", 0))
    try:
        source_fd = os.open(source, source_flags)
    except OSError as exc:
        raise ArtifactRegistryError(f"cannot open source artifact safely: {source.name}") from exc
    destination_fd: int | None = None
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ArtifactRegistryError("registry source files must be regular and unlinked")
        destination_fd = os.open(destination, destination_flags, 0o600)
        remaining = before.st_size
        while remaining:
            chunk = os.read(source_fd, min(1024 * 1024, remaining))
            if not chunk:
                raise ArtifactRegistryError("source artifact changed while being copied")
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                view = view[written:]
            remaining -= len(chunk)
        after = os.fstat(source_fd)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after:
            raise ArtifactRegistryError("source artifact changed while being copied")
        os.fsync(destination_fd)
    finally:
        os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)
    if os.name != "nt":
        os.chmod(destination, 0o444)


def _copy_regular_tree(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise ArtifactRegistryError("registry source bundle must be a real directory")
    destination.mkdir(mode=0o700)
    pending = [(source, destination)]
    entry_count = 0
    directories: list[Path] = [destination]
    while pending:
        source_dir, destination_dir = pending.pop()
        try:
            entries = sorted(os.scandir(source_dir), key=lambda item: item.name)
        except OSError as exc:
            raise ArtifactRegistryError("cannot scan source bundle safely") from exc
        for entry in entries:
            entry_count += 1
            if entry_count > MAX_TREE_ENTRIES:
                raise ArtifactRegistryError("source bundle contains too many entries")
            source_path = Path(entry.path)
            destination_path = destination_dir / entry.name
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ArtifactRegistryError("cannot inspect source bundle safely") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise ArtifactRegistryError("registry source bundles must not contain symlinks")
            if stat.S_ISDIR(metadata.st_mode):
                destination_path.mkdir(mode=0o700)
                directories.append(destination_path)
                pending.append((source_path, destination_path))
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ArtifactRegistryError("registry source bundles contain an unsafe file type")
            _copy_file(source_path, destination_path)
    if os.name != "nt":
        for directory in reversed(directories):
            directory.chmod(0o555)


def _artifact_directory(root: Path, artifact_id: str, *, strict: bool = True) -> Path:
    if not SHA256_RE.fullmatch(artifact_id):
        raise ArtifactRegistryError("invalid artifact ID")
    objects = (root / OBJECTS_DIRNAME).resolve(strict=True)
    candidate = objects / artifact_id
    if candidate.is_symlink():
        raise ArtifactRegistryError("artifact directory must not be a symlink")
    try:
        resolved = candidate.resolve(strict=strict)
    except OSError as exc:
        raise ArtifactRegistryError("artifact does not exist") from exc
    if resolved.parent != objects:
        raise ArtifactRegistryError("artifact must be a direct registry object")
    if strict and not resolved.is_dir():
        raise ArtifactRegistryError("artifact must be a directory")
    return resolved


def _require_frozen_tree(root: Path) -> None:
    entry_count = 0
    pending = [root]
    while pending:
        current = pending.pop()
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ArtifactRegistryError("registry artifacts must not contain symlinks")
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o222:
            raise ArtifactRegistryError("registry artifact tree must be read-only")
        if stat.S_ISDIR(metadata.st_mode):
            children = list(current.iterdir())
            entry_count += len(children)
            if entry_count > MAX_TREE_ENTRIES:
                raise ArtifactRegistryError("registry artifact contains too many entries")
            pending.extend(children)
        elif not stat.S_ISREG(metadata.st_mode):
            raise ArtifactRegistryError("registry artifact contains an unsafe file type")


def _verify_artifact_dir(
    artifact_dir: Path,
    *,
    expected_artifact_id: str,
    trusted_package_keys: Path,
    trusted_runtime_keys: Path,
    python_executable: str,
) -> ArtifactPublication:
    if artifact_dir.is_symlink() or not artifact_dir.is_dir():
        raise ArtifactRegistryError("artifact directory is missing or unsafe")
    expected_files = {PUBLICATION_FILENAME, PACKAGE_DIRNAME, RUNTIME_DIRNAME}
    if {item.name for item in artifact_dir.iterdir()} != expected_files:
        raise ArtifactRegistryError("artifact directory file set is invalid")
    _require_frozen_tree(artifact_dir)
    publication = load_publication(artifact_dir)
    if publication.artifact_id != expected_artifact_id:
        raise ArtifactRegistryError("publication record belongs to another artifact")

    package_manifest, package_signature = verify_signed_package_bundle(
        artifact_dir / PACKAGE_DIRNAME,
        trusted_keys_dir=trusted_package_keys,
    )
    runtime_lock, runtime_signature = verify_signed_runtime_bundle(
        artifact_dir / RUNTIME_DIRNAME,
        package_bundle=artifact_dir / PACKAGE_DIRNAME,
        trusted_package_keys=trusted_package_keys,
        trusted_runtime_keys=trusted_runtime_keys,
        python_executable=python_executable,
    )
    expected = ArtifactPublication(
        artifact_id=artifact_id_for(
            package_signature.manifest_sha256,
            runtime_signature.lock_sha256,
        ),
        package_version=package_manifest.package_version,
        package_manifest_sha256=package_signature.manifest_sha256,
        package_signing_key_id=package_signature.key_id,
        runtime_lock_sha256=runtime_signature.lock_sha256,
        runtime_signing_key_id=runtime_signature.key_id,
        package_commit_sha=package_manifest.commit_sha,
        package_git_tree_sha=package_manifest.git_tree_sha,
        target=runtime_lock.target,
        wheel_count=len(runtime_lock.wheels),
    )
    if publication != expected:
        raise ArtifactRegistryError("publication record does not match trusted bundles")
    if {item.name for item in artifact_dir.iterdir()} != expected_files:
        raise ArtifactRegistryError("artifact directory changed during verification")
    return publication


def publish_artifact(
    *,
    registry_root: Path,
    package_bundle: Path,
    runtime_bundle: Path,
    trusted_package_keys: Path,
    trusted_runtime_keys: Path,
    python_executable: str = sys.executable,
) -> ArtifactPublication:
    root = _validate_registry_path(registry_root)
    _require_external_trust(root, trusted_package_keys, label="trusted package keys")
    _require_external_trust(root, trusted_runtime_keys, label="trusted runtime keys")
    package_manifest, package_signature = verify_signed_package_bundle(
        package_bundle,
        trusted_keys_dir=trusted_package_keys,
    )
    runtime_lock, runtime_signature = verify_signed_runtime_bundle(
        runtime_bundle,
        package_bundle=package_bundle,
        trusted_package_keys=trusted_package_keys,
        trusted_runtime_keys=trusted_runtime_keys,
        python_executable=python_executable,
    )
    publication = ArtifactPublication(
        artifact_id=artifact_id_for(
            package_signature.manifest_sha256,
            runtime_signature.lock_sha256,
        ),
        package_version=package_manifest.package_version,
        package_manifest_sha256=package_signature.manifest_sha256,
        package_signing_key_id=package_signature.key_id,
        runtime_lock_sha256=runtime_signature.lock_sha256,
        runtime_signing_key_id=runtime_signature.key_id,
        package_commit_sha=package_manifest.commit_sha,
        package_git_tree_sha=package_manifest.git_tree_sha,
        target=runtime_lock.target,
        wheel_count=len(runtime_lock.wheels),
    )
    objects = root / OBJECTS_DIRNAME
    target = _artifact_directory(root, publication.artifact_id, strict=False)
    with _registry_lock(root):
        if target.exists() or target.is_symlink():
            existing = verify_registry_artifact(
                registry_root=root,
                artifact_id=publication.artifact_id,
                trusted_package_keys=trusted_package_keys,
                trusted_runtime_keys=trusted_runtime_keys,
                python_executable=python_executable,
            )
            if existing != publication:
                raise ArtifactRegistryError("existing artifact identity has different metadata")
            return existing

        temp = objects / f".tmp.{os.getpid()}.{uuid.uuid4().hex}"
        try:
            temp.mkdir(mode=0o700)
            _copy_regular_tree(package_bundle.resolve(strict=True), temp / PACKAGE_DIRNAME)
            _copy_regular_tree(runtime_bundle.resolve(strict=True), temp / RUNTIME_DIRNAME)
            _write_publication(temp / PUBLICATION_FILENAME, publication)
            if os.name != "nt":
                temp.chmod(0o555)
            _verify_artifact_dir(
                temp,
                expected_artifact_id=publication.artifact_id,
                trusted_package_keys=trusted_package_keys,
                trusted_runtime_keys=trusted_runtime_keys,
                python_executable=python_executable,
            )
            os.replace(temp, target)
        except Exception:
            if temp.exists() and not temp.is_symlink():
                if os.name != "nt":
                    for path in temp.rglob("*"):
                        try:
                            path.chmod(0o700 if path.is_dir() else 0o600)
                        except OSError:
                            pass
                    temp.chmod(0o700)
                shutil.rmtree(temp, ignore_errors=True)
            raise

    return verify_registry_artifact(
        registry_root=root,
        artifact_id=publication.artifact_id,
        trusted_package_keys=trusted_package_keys,
        trusted_runtime_keys=trusted_runtime_keys,
        python_executable=python_executable,
    )


def verify_registry_artifact(
    *,
    registry_root: Path,
    artifact_id: str,
    trusted_package_keys: Path,
    trusted_runtime_keys: Path,
    python_executable: str = sys.executable,
) -> ArtifactPublication:
    root = _validate_registry_path(registry_root)
    _require_external_trust(root, trusted_package_keys, label="trusted package keys")
    _require_external_trust(root, trusted_runtime_keys, label="trusted runtime keys")
    artifact_dir = _artifact_directory(root, artifact_id)
    return _verify_artifact_dir(
        artifact_dir,
        expected_artifact_id=artifact_id,
        trusted_package_keys=trusted_package_keys,
        trusted_runtime_keys=trusted_runtime_keys,
        python_executable=python_executable,
    )


def _print_publication(
    publication: ArtifactPublication,
    *,
    registry_root: Path,
    json_output: bool,
) -> None:
    artifact_dir = registry_root / OBJECTS_DIRNAME / publication.artifact_id
    payload = {
        **publication.model_dump(mode="json"),
        "artifact_path": str(artifact_dir),
        "package_path": str(artifact_dir / PACKAGE_DIRNAME),
        "runtime_path": str(artifact_dir / RUNTIME_DIRNAME),
    }
    if json_output:
        print(json.dumps(payload, sort_keys=True))
        return
    print(f"artifact_id: {publication.artifact_id}")
    print(f"package_version: {publication.package_version}")
    print(f"artifact_path: {artifact_dir}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publish trusted Remote MCP Commander artifacts to an immutable registry"
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--root", required=True)
    init_parser.add_argument("--json", action="store_true", dest="json_output")
    publish_parser = subparsers.add_parser("publish")
    publish_parser.add_argument("--root", required=True)
    publish_parser.add_argument("--package-bundle", required=True)
    publish_parser.add_argument("--runtime-bundle", required=True)
    publish_parser.add_argument("--trusted-package-keys", required=True)
    publish_parser.add_argument("--trusted-runtime-keys", required=True)
    publish_parser.add_argument("--python", default=sys.executable)
    publish_parser.add_argument("--json", action="store_true", dest="json_output")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--root", required=True)
    verify_parser.add_argument("--artifact-id", required=True)
    verify_parser.add_argument("--trusted-package-keys", required=True)
    verify_parser.add_argument("--trusted-runtime-keys", required=True)
    verify_parser.add_argument("--python", default=sys.executable)
    verify_parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)

    try:
        root = Path(args.root).expanduser()
        if args.action == "init":
            result = init_registry(root)
            if args.json_output:
                print(json.dumps({"root": str(result)}, sort_keys=True))
            else:
                print(f"registry root: {result}")
            return 0
        if args.action == "publish":
            publication = publish_artifact(
                registry_root=root,
                package_bundle=Path(args.package_bundle),
                runtime_bundle=Path(args.runtime_bundle),
                trusted_package_keys=Path(args.trusted_package_keys),
                trusted_runtime_keys=Path(args.trusted_runtime_keys),
                python_executable=args.python,
            )
        else:
            publication = verify_registry_artifact(
                registry_root=root,
                artifact_id=args.artifact_id,
                trusted_package_keys=Path(args.trusted_package_keys),
                trusted_runtime_keys=Path(args.trusted_runtime_keys),
                python_executable=args.python,
            )
        _print_publication(publication, registry_root=root, json_output=args.json_output)
        return 0
    except (
        OSError,
        ArtifactRegistryError,
        PackagingError,
        PackageSigningError,
        RuntimeLockError,
        RuntimeSigningError,
    ) as exc:
        if getattr(args, "json_output", False):
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        else:
            print(f"artifact registry error: {exc}")
        return 1


def run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    run()

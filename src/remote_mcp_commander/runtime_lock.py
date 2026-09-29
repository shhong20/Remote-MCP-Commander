from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile
from email.parser import BytesParser
from email.policy import compat32
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from remote_mcp_commander.package import (
    MAX_ARTIFACT_BYTES,
    PACKAGE_MANIFEST,
    PackageManifest,
    PackagingError,
    _read_small_regular,
    _sha256_file,
)
from remote_mcp_commander.package_signing import (
    PackageSigningError,
    verify_signed_package_bundle,
)

RUNTIME_LOCK_FILENAME = "runtime-lock.json"
WHEELHOUSE_DIRNAME = "wheelhouse"
RUNTIME_LOCK_SCHEMA_VERSION = 1
MAX_LOCK_BYTES = 4 * 1024 * 1024
MAX_RUNTIME_WHEELS = 256
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_WHEEL_ARCHIVE_ENTRIES = 10_000
SUPPORTED_IMPLEMENTATION = "cpython"
SUPPORTED_PYTHON = "3.11"
SUPPORTED_SYSTEM = "linux"
SUPPORTED_MACHINE = "x86_64"
SUPPORTED_PIP = "26.2.1"
WHEEL_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,255}\.whl$")
SHA256_RE = r"^[a-f0-9]{64}$"


class RuntimeLockError(RuntimeError):
    pass


class RuntimeTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    implementation: Literal["cpython"] = SUPPORTED_IMPLEMENTATION
    python_version: Literal["3.11"] = SUPPORTED_PYTHON
    system: Literal["linux"] = SUPPORTED_SYSTEM
    machine: Literal["x86_64"] = SUPPORTED_MACHINE
    platform_tag: str = Field(min_length=1, max_length=256)
    pip_version: Literal["26.2.1"] = SUPPORTED_PIP


class LockedWheel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    normalized_name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    version: str = Field(min_length=1, max_length=128)
    filename: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,255}\.whl$")
    sha256: str = Field(pattern=SHA256_RE)
    size: int = Field(ge=1, le=MAX_ARTIFACT_BYTES)
    tags: list[str] = Field(min_length=1, max_length=64)


class RuntimeLock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = RUNTIME_LOCK_SCHEMA_VERSION
    package_manifest_sha256: str = Field(pattern=SHA256_RE)
    package_signing_key_id: str = Field(min_length=1, max_length=64)
    package_commit_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    package_git_tree_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    package_version: str = Field(min_length=1, max_length=128)
    target: RuntimeTarget
    resolver: dict[str, str | bool]
    wheels: list[LockedWheel] = Field(min_length=1, max_length=MAX_RUNTIME_WHEELS)

    @model_validator(mode="after")
    def validate_lock(self) -> RuntimeLock:
        names = [item.normalized_name for item in self.wheels]
        if names != sorted(names):
            raise ValueError("runtime wheels must be sorted by normalized name")
        if len(names) != len(set(names)):
            raise ValueError("runtime lock contains duplicate project names")
        if "remote-mcp-commander" not in names:
            raise ValueError("runtime lock must contain remote-mcp-commander")
        expected_resolver = {
            "pip": self.target.pip_version,
            "only_binary": True,
        }
        if self.resolver != expected_resolver:
            raise ValueError("runtime resolver metadata does not match the target")
        return self


def _normalize_name(name: str) -> str:
    normalized = re.sub(r"[-_.]+", "-", name).lower().strip("-")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", normalized):
        raise RuntimeLockError(f"invalid wheel project name: {name}")
    return normalized


def _run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    error_message: str,
) -> bytes:
    try:
        result = subprocess.run(
            command,
            env=env,
            cwd=cwd,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeLockError(error_message) from exc
    return result.stdout


def _runtime_target(python_executable: str) -> RuntimeTarget:
    script = (
        "import importlib.metadata as m,json,platform,sys,sysconfig;"
        "print(json.dumps({"
        "'implementation':sys.implementation.name,"
        "'python_version':f'{sys.version_info.major}.{sys.version_info.minor}',"
        "'system':platform.system().lower(),"
        "'machine':platform.machine().lower(),"
        "'platform_tag':sysconfig.get_platform(),"
        "'pip_version':m.version('pip')},sort_keys=True))"
    )
    raw = _run(
        [python_executable, "-c", script],
        error_message="cannot inspect runtime Python target",
    )
    try:
        payload = json.loads(raw.decode("utf-8"))
        target = RuntimeTarget.model_validate(payload)
    except Exception as exc:
        raise RuntimeLockError(
            "runtime target must be CPython 3.11 on Linux x86_64 with pip 26.2.1"
        ) from exc
    return target


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeLockError(f"duplicate runtime-lock JSON key: {key}")
        result[key] = value
    return result


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _write_lock(path: Path, lock: RuntimeLock) -> None:
    data = _canonical_json_bytes(lock.model_dump(mode="json"))
    if len(data) > MAX_LOCK_BYTES:
        raise RuntimeLockError("runtime lock exceeds the size limit")
    path.write_bytes(data)
    if os.name != "nt":
        path.chmod(0o444)


def load_runtime_lock(runtime_bundle: Path) -> RuntimeLock:
    path = runtime_bundle / RUNTIME_LOCK_FILENAME
    if os.name != "nt":
        try:
            if stat.S_IMODE(path.stat().st_mode) & 0o222:
                raise RuntimeLockError("runtime lock must be read-only")
        except OSError as exc:
            raise RuntimeLockError("runtime lock is missing or unsafe") from exc
    try:
        raw = _read_small_regular(
            path,
            limit=MAX_LOCK_BYTES,
            label="runtime lock",
        )
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        return RuntimeLock.model_validate(payload)
    except RuntimeLockError:
        raise
    except Exception as exc:
        raise RuntimeLockError("runtime lock is invalid") from exc


def _manifest_sha256(package_bundle: Path) -> str:
    try:
        raw = _read_small_regular(
            package_bundle / PACKAGE_MANIFEST,
            limit=4 * 1024 * 1024,
            label="package manifest",
        )
    except PackagingError as exc:
        raise RuntimeLockError("cannot read trusted package manifest") from exc
    return hashlib.sha256(raw).hexdigest()


def _wheel_metadata(path: Path) -> LockedWheel:
    if path.is_symlink() or not path.is_file() or not WHEEL_FILENAME_RE.fullmatch(path.name):
        raise RuntimeLockError(f"invalid wheelhouse entry: {path.name}")
    try:
        digest, size = _sha256_file(path)
    except PackagingError as exc:
        raise RuntimeLockError(f"cannot hash wheel safely: {path.name}") from exc
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            metadata_paths = [name for name in names if name.endswith(".dist-info/METADATA")]
            wheel_paths = [name for name in names if name.endswith(".dist-info/WHEEL")]
            if len(metadata_paths) != 1 or len(wheel_paths) != 1:
                raise RuntimeLockError(f"wheel metadata layout is invalid: {path.name}")
            metadata_info = archive.getinfo(metadata_paths[0])
            wheel_info = archive.getinfo(wheel_paths[0])
            if (
                metadata_info.file_size > MAX_METADATA_BYTES
                or wheel_info.file_size > MAX_METADATA_BYTES
            ):
                raise RuntimeLockError(f"wheel metadata is too large: {path.name}")
            metadata_raw = archive.read(metadata_info)
            wheel_raw = archive.read(wheel_info)
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise RuntimeLockError(f"cannot inspect wheel metadata: {path.name}") from exc

    metadata = BytesParser(policy=compat32).parsebytes(metadata_raw)
    wheel_metadata = BytesParser(policy=compat32).parsebytes(wheel_raw)
    name = metadata.get("Name")
    version = metadata.get("Version")
    tags = wheel_metadata.get_all("Tag") or []
    if not name or not version or not tags:
        raise RuntimeLockError(f"wheel is missing Name/Version/Tag metadata: {path.name}")
    unique_tags = sorted(set(str(tag) for tag in tags))
    return LockedWheel(
        name=str(name),
        normalized_name=_normalize_name(str(name)),
        version=str(version),
        filename=path.name,
        sha256=digest,
        size=size,
        tags=unique_tags,
    )


def _wheelhouse_records(wheelhouse: Path) -> list[LockedWheel]:
    if wheelhouse.is_symlink() or not wheelhouse.is_dir():
        raise RuntimeLockError("wheelhouse must be a real directory")
    if os.name != "nt" and stat.S_IMODE(wheelhouse.stat().st_mode) & 0o022:
        raise RuntimeLockError("wheelhouse must not be group/world writable")
    entries = list(wheelhouse.iterdir())
    if not entries or len(entries) > MAX_RUNTIME_WHEELS:
        raise RuntimeLockError("wheelhouse entry count is outside allowed bounds")
    if any(item.is_dir() or not item.name.endswith(".whl") for item in entries):
        raise RuntimeLockError("wheelhouse may contain wheel files only")
    records = [_wheel_metadata(item) for item in entries]
    records.sort(key=lambda item: item.normalized_name)
    names = [item.normalized_name for item in records]
    if len(names) != len(set(names)):
        raise RuntimeLockError("wheelhouse contains duplicate project names")
    return records


def _package_wheel_record(
    manifest: PackageManifest,
    records: list[LockedWheel],
) -> LockedWheel:
    package_artifact = next(
        (item for item in manifest.artifacts if item.kind == "wheel"),
        None,
    )
    if package_artifact is None:
        raise RuntimeLockError("trusted package manifest does not contain a wheel")
    matches = [item for item in records if item.normalized_name == "remote-mcp-commander"]
    if len(matches) != 1:
        raise RuntimeLockError("wheelhouse must contain exactly one remote-mcp-commander wheel")
    record = matches[0]
    if (
        record.filename != package_artifact.filename
        or record.sha256 != package_artifact.sha256
        or record.size != package_artifact.size
        or record.version != manifest.package_version
    ):
        raise RuntimeLockError("runtime package wheel does not match the trusted package manifest")
    return record


def _pip_env(home: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "HOME": str(home),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_CACHE_DIR": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def create_runtime_bundle(
    *,
    package_bundle: Path,
    trusted_package_keys: Path,
    output: Path,
    python_executable: str = sys.executable,
) -> RuntimeLock:
    try:
        package_manifest, package_signature = verify_signed_package_bundle(
            package_bundle,
            trusted_keys_dir=trusted_package_keys,
        )
    except (PackagingError, PackageSigningError) as exc:
        raise RuntimeLockError("package bundle is not trusted") from exc
    package_bundle = package_bundle.resolve(strict=True)
    target = _runtime_target(python_executable)

    package_artifact = next(
        (item for item in package_manifest.artifacts if item.kind == "wheel"),
        None,
    )
    if package_artifact is None:
        raise RuntimeLockError("trusted package manifest does not contain a wheel")
    package_wheel = package_bundle / package_artifact.filename

    output = output.expanduser()
    try:
        parent = output.parent.resolve(strict=True)
    except OSError as exc:
        raise RuntimeLockError("runtime output parent directory does not exist") from exc
    if not parent.is_dir():
        raise RuntimeLockError("runtime output parent must be a directory")
    output = parent / output.name
    if output.exists() or output.is_symlink():
        raise RuntimeLockError("runtime output path already exists")

    temp = parent / f".{output.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    try:
        temp.mkdir(mode=0o700)
        wheelhouse = temp / WHEELHOUSE_DIRNAME
        wheelhouse.mkdir(mode=0o700)
        with tempfile.TemporaryDirectory(prefix="rmc-runtime-home-") as raw_home:
            env = _pip_env(Path(raw_home))
            _run(
                [
                    python_executable,
                    "-m",
                    "pip",
                    "download",
                    "--disable-pip-version-check",
                    "--only-binary=:all:",
                    "--dest",
                    str(wheelhouse),
                    str(package_wheel),
                ],
                env=env,
                error_message="runtime dependency wheel resolution failed",
            )
        records = _wheelhouse_records(wheelhouse)
        _package_wheel_record(package_manifest, records)
        lock = RuntimeLock(
            package_manifest_sha256=_manifest_sha256(package_bundle),
            package_signing_key_id=package_signature.key_id,
            package_commit_sha=package_manifest.commit_sha,
            package_git_tree_sha=package_manifest.git_tree_sha,
            package_version=package_manifest.package_version,
            target=target,
            resolver={"pip": target.pip_version, "only_binary": True},
            wheels=records,
        )
        _write_lock(temp / RUNTIME_LOCK_FILENAME, lock)
        verify_runtime_bundle(
            runtime_bundle=temp,
            package_bundle=package_bundle,
            trusted_package_keys=trusted_package_keys,
            python_executable=python_executable,
        )
        os.replace(temp, output)
        return lock
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def _offline_resolution(
    *,
    python_executable: str,
    wheelhouse: Path,
    package_wheel: LockedWheel,
) -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="rmc-runtime-verify-") as raw_temp:
        temp = Path(raw_temp)
        report = temp / "pip-report.json"
        env = _pip_env(temp / "home")
        env["PIP_NO_INDEX"] = "1"
        env["PIP_CONFIG_FILE"] = os.devnull
        _run(
            [
                python_executable,
                "-m",
                "pip",
                "install",
                "--dry-run",
                "--ignore-installed",
                "--no-index",
                "--only-binary=:all:",
                "--find-links",
                str(wheelhouse),
                "--report",
                str(report),
                str(wheelhouse / package_wheel.filename),
            ],
            env=env,
            error_message="offline runtime dependency resolution failed",
        )
        try:
            payload = json.loads(report.read_text(encoding="utf-8"))
            installs = payload["install"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeLockError("pip dry-run report is invalid") from exc
    resolved: dict[str, str] = {}
    for item in installs:
        try:
            metadata = item["metadata"]
            name = _normalize_name(str(metadata["name"]))
            version = str(metadata["version"])
        except (KeyError, TypeError) as exc:
            raise RuntimeLockError("pip dry-run report lacks package metadata") from exc
        if name in resolved:
            raise RuntimeLockError("pip dry-run resolved a duplicate project")
        resolved[name] = version
    return resolved


def verify_runtime_bundle(
    *,
    runtime_bundle: Path,
    package_bundle: Path,
    trusted_package_keys: Path,
    python_executable: str = sys.executable,
) -> RuntimeLock:
    runtime_input = runtime_bundle.expanduser()
    if runtime_input.is_symlink():
        raise RuntimeLockError("runtime bundle must not be a symlink")
    try:
        runtime_bundle = runtime_input.resolve(strict=True)
    except OSError as exc:
        raise RuntimeLockError("runtime bundle does not exist") from exc
    if not runtime_bundle.is_dir():
        raise RuntimeLockError("runtime bundle must be a directory")
    if {item.name for item in runtime_bundle.iterdir()} != {
        RUNTIME_LOCK_FILENAME,
        WHEELHOUSE_DIRNAME,
    }:
        raise RuntimeLockError("runtime bundle file set is invalid")

    lock = load_runtime_lock(runtime_bundle)
    current_target = _runtime_target(python_executable)
    if current_target != lock.target:
        raise RuntimeLockError("runtime target does not match the lock")

    try:
        package_manifest, package_signature = verify_signed_package_bundle(
            package_bundle,
            trusted_keys_dir=trusted_package_keys,
        )
    except (PackagingError, PackageSigningError) as exc:
        raise RuntimeLockError("package bundle is not trusted") from exc
    package_bundle = package_bundle.resolve(strict=True)
    if _manifest_sha256(package_bundle) != lock.package_manifest_sha256:
        raise RuntimeLockError("runtime lock references a different package manifest")
    if package_signature.key_id != lock.package_signing_key_id:
        raise RuntimeLockError("runtime lock references a different package signing key")
    if (
        package_manifest.commit_sha != lock.package_commit_sha
        or package_manifest.git_tree_sha != lock.package_git_tree_sha
        or package_manifest.package_version != lock.package_version
    ):
        raise RuntimeLockError("runtime lock package identity does not match the trusted package")

    wheelhouse = runtime_bundle / WHEELHOUSE_DIRNAME
    records = _wheelhouse_records(wheelhouse)
    if records != lock.wheels:
        raise RuntimeLockError("runtime wheelhouse does not match the lock")
    package_record = _package_wheel_record(package_manifest, records)
    resolved = _offline_resolution(
        python_executable=python_executable,
        wheelhouse=wheelhouse,
        package_wheel=package_record,
    )
    locked_versions = {item.normalized_name: item.version for item in lock.wheels}
    if resolved != locked_versions:
        raise RuntimeLockError("offline dependency closure does not match the runtime lock")

    if {item.name for item in runtime_bundle.iterdir()} != {
        RUNTIME_LOCK_FILENAME,
        WHEELHOUSE_DIRNAME,
    }:
        raise RuntimeLockError("runtime bundle changed during verification")
    return lock


def _print_lock(lock: RuntimeLock, *, json_output: bool) -> None:
    payload = lock.model_dump(mode="json")
    if json_output:
        print(json.dumps(payload, sort_keys=True))
        return
    print(f"package: {lock.package_version} commit={lock.package_commit_sha}")
    print(
        f"target: {lock.target.implementation} {lock.target.python_version} "
        f"{lock.target.system}/{lock.target.machine}"
    )
    print(f"wheels: {len(lock.wheels)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Lock and verify Remote MCP Commander runtime wheels"
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    lock_parser = subparsers.add_parser("lock")
    lock_parser.add_argument("--package-bundle", required=True)
    lock_parser.add_argument("--trusted-package-keys", required=True)
    lock_parser.add_argument("--output", required=True)
    lock_parser.add_argument("--python", default=sys.executable)
    lock_parser.add_argument("--json", action="store_true", dest="json_output")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--runtime-bundle", required=True)
    verify_parser.add_argument("--package-bundle", required=True)
    verify_parser.add_argument("--trusted-package-keys", required=True)
    verify_parser.add_argument("--python", default=sys.executable)
    verify_parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    try:
        if args.action == "lock":
            lock = create_runtime_bundle(
                package_bundle=Path(args.package_bundle),
                trusted_package_keys=Path(args.trusted_package_keys),
                output=Path(args.output),
                python_executable=args.python,
            )
        else:
            lock = verify_runtime_bundle(
                runtime_bundle=Path(args.runtime_bundle),
                package_bundle=Path(args.package_bundle),
                trusted_package_keys=Path(args.trusted_package_keys),
                python_executable=args.python,
            )
    except RuntimeLockError as exc:
        if args.json_output:
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        else:
            print(f"runtime lock error: {exc}")
        return 1
    _print_lock(lock, json_output=args.json_output)
    return 0


def run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    run()

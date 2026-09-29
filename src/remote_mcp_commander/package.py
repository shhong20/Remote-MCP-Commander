from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BUILD_CONTRACT_VERSION = 1
BUILDER_PYTHON_MINOR = "3.11"
BUILD_TOOLCHAIN = {
    "build": "1.6.1",
    "setuptools": "84.0.0",
    "wheel": "0.48.0",
}
PACKAGE_MANIFEST = "package-manifest.json"
PACKAGE_SIGNATURE = "package-manifest.sig.json"
MAX_SOURCE_ENTRIES = 20_000
MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_USTAR_NAME_BYTES = 100
MAX_USTAR_LINK_BYTES = 100
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,199}$")
SHA_RE = re.compile(r"^[a-f0-9]{40}$")


class PackagingError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitEntry:
    path: str
    mode: int
    kind: Literal["file", "symlink"]
    data: bytes


@dataclass(frozen=True)
class GitSnapshot:
    commit_sha: str
    tree_sha: str
    source_date_epoch: int
    package_version: str
    protocol_min: int
    protocol_max: int
    entries: tuple[GitEntry, ...]


class ArtifactRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["source", "wheel"]
    filename: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=1, le=MAX_ARTIFACT_BYTES)


class PackageManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    build_contract_version: Literal[1] = BUILD_CONTRACT_VERSION
    commit_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    git_tree_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    source_date_epoch: int = Field(ge=1)
    package_version: str = Field(min_length=1, max_length=128)
    protocol_min: int = Field(ge=1, le=65_535)
    protocol_max: int = Field(ge=1, le=65_535)
    builder_python: Literal["3.11"] = BUILDER_PYTHON_MINOR
    toolchain: dict[str, str]
    reproducibility_verified: Literal[True] = True
    artifacts: list[ArtifactRecord] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def validate_contract(self) -> PackageManifest:
        if self.protocol_min > self.protocol_max:
            raise ValueError("protocol_min must be <= protocol_max")
        if self.toolchain != BUILD_TOOLCHAIN:
            raise ValueError("package manifest toolchain does not match the build contract")
        if [item.kind for item in self.artifacts] != ["source", "wheel"]:
            raise ValueError("package manifest must contain source then wheel artifacts")
        if len({item.filename for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("package artifact filenames must be unique")
        return self


def _run(
    command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
) -> bytes:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            detail = exc.stderr.decode("utf-8", errors="replace")[-2000:].strip()
        message = "subprocess failed"
        if detail:
            message += f": {detail}"
        raise PackagingError(message) from exc
    return result.stdout


def _validate_repo(raw_repo: str | Path) -> Path:
    repo = Path(raw_repo).expanduser()
    if repo.is_symlink():
        raise PackagingError("repository path must not be a symlink")
    try:
        repo = repo.resolve(strict=True)
    except OSError as exc:
        raise PackagingError("repository path does not exist") from exc
    if not repo.is_dir():
        raise PackagingError("repository path must be a directory")
    top = Path(_run(["git", "-C", str(repo), "rev-parse", "--show-toplevel"]).decode().strip())
    if top.resolve() != repo:
        raise PackagingError("repository path must be the Git worktree root")
    return repo


def _canonical_commit(repo: Path, raw_ref: str) -> str:
    if not REF_RE.fullmatch(raw_ref) or raw_ref.startswith("-") or ".." in raw_ref:
        raise PackagingError("invalid commit/ref syntax")
    value = (
        _run(["git", "-C", str(repo), "rev-parse", "--verify", f"{raw_ref}^{{commit}}"])
        .decode()
        .strip()
    )
    if not SHA_RE.fullmatch(value):
        raise PackagingError("resolved commit is not a SHA-1 commit ID")
    return value


def _safe_git_path(raw: bytes) -> str:
    try:
        path = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PackagingError("source paths must be UTF-8") from exc
    pure = PurePosixPath(path)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise PackagingError("unsafe path in Git tree")
    if "\x00" in path:
        raise PackagingError("NUL is not allowed in Git paths")
    return path


def _literal_assignment(source: bytes, name: str) -> object:
    try:
        tree = ast.parse(source.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise PackagingError(f"cannot parse metadata source for {name}") from exc
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    try:
                        return ast.literal_eval(node.value)
                    except (ValueError, TypeError) as exc:
                        raise PackagingError(f"metadata value for {name} must be literal") from exc
    raise PackagingError(f"missing metadata value: {name}")


def _snapshot(repo: Path, raw_ref: str) -> GitSnapshot:
    commit = _canonical_commit(repo, raw_ref)
    tree_sha = _run(["git", "-C", str(repo), "rev-parse", f"{commit}^{{tree}}"]).decode().strip()
    if not SHA_RE.fullmatch(tree_sha):
        raise PackagingError("resolved Git tree is not a SHA-1 tree ID")
    epoch_raw = (
        _run(["git", "-C", str(repo), "show", "-s", "--format=%ct", commit]).decode().strip()
    )
    try:
        epoch = int(epoch_raw)
    except ValueError as exc:
        raise PackagingError("commit timestamp is invalid") from exc
    if epoch < 1:
        raise PackagingError("commit timestamp must be positive")

    records = _run(["git", "-C", str(repo), "ls-tree", "-rz", "--full-tree", commit]).split(b"\0")
    entries: list[GitEntry] = []
    total = 0
    blobs: dict[str, bytes] = {}
    for record in records:
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode_raw, object_type, oid = header.split(b" ", 2)
        except ValueError as exc:
            raise PackagingError("invalid git ls-tree record") from exc
        if object_type != b"blob":
            raise PackagingError("Git submodules and non-blob tree entries are not packageable")
        path = _safe_git_path(raw_path)
        if len(entries) >= MAX_SOURCE_ENTRIES:
            raise PackagingError("source snapshot contains too many entries")
        try:
            mode_text = mode_raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise PackagingError("invalid Git mode") from exc
        if mode_text not in {"100644", "100755", "120000"}:
            raise PackagingError(f"unsupported Git mode for source entry: {path}")
        oid_text = oid.decode("ascii")
        data = _run(["git", "-C", str(repo), "cat-file", "blob", oid_text])
        total += len(data)
        if total > MAX_SOURCE_BYTES:
            raise PackagingError("source snapshot exceeds package byte limit")
        kind: Literal["file", "symlink"] = "symlink" if mode_text == "120000" else "file"
        mode = 0o777 if kind == "symlink" else (0o755 if mode_text == "100755" else 0o644)
        if kind == "symlink":
            try:
                target = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PackagingError(f"symlink target must be UTF-8: {path}") from exc
            target_path = PurePosixPath(target)
            if target_path.is_absolute() or ".." in target_path.parts or "\x00" in target:
                raise PackagingError(f"source symlink must stay inside the source tree: {path}")
        entries.append(GitEntry(path=path, mode=mode, kind=kind, data=data))
        blobs[path] = data

    required = {"pyproject.toml", "src/remote_mcp_commander/protocol.py"}
    missing = sorted(required - blobs.keys())
    if missing:
        raise PackagingError(f"source snapshot is missing required metadata: {', '.join(missing)}")
    try:
        pyproject = tomllib.loads(blobs["pyproject.toml"].decode("utf-8"))
        project_name = str(pyproject["project"]["name"])
        package_version = str(pyproject["project"]["version"])
        build_system = pyproject["build-system"]
        build_backend = str(build_system["build-backend"])
        build_requires = [str(item) for item in build_system["requires"]]
    except (UnicodeDecodeError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise PackagingError("cannot read package/build metadata from source snapshot") from exc
    if project_name != "remote-mcp-commander":
        raise PackagingError("project name must remain remote-mcp-commander")
    if build_backend != "setuptools.build_meta":
        raise PackagingError("package build backend must remain setuptools.build_meta")
    project_dynamic = pyproject.get("project", {}).get("dynamic", [])
    setuptools_dynamic = pyproject.get("tool", {}).get("setuptools", {}).get("dynamic", {})
    if project_dynamic or setuptools_dynamic:
        raise PackagingError("dynamic package metadata is not allowed by the build contract")
    if any(path in blobs for path in {"setup.py", "setup.cfg"}):
        raise PackagingError("legacy setup.py/setup.cfg build configuration is not packageable")
    expected_backend = "setuptools==84.0.0"
    if build_requires != [expected_backend]:
        raise PackagingError("build-system requirements do not match the reproducible contract")
    protocol_source = blobs["src/remote_mcp_commander/protocol.py"]
    protocol_min = _literal_assignment(protocol_source, "PROTOCOL_MIN_SUPPORTED")
    protocol_max = _literal_assignment(protocol_source, "PROTOCOL_MAX_SUPPORTED")
    if not isinstance(protocol_min, int) or not isinstance(protocol_max, int):
        raise PackagingError("protocol metadata must be integers")
    if protocol_min < 1 or protocol_max < protocol_min or protocol_max > 65_535:
        raise PackagingError("protocol metadata is invalid")

    entries.sort(key=lambda item: item.path)
    return GitSnapshot(
        commit_sha=commit,
        tree_sha=tree_sha,
        source_date_epoch=epoch,
        package_version=package_version,
        protocol_min=protocol_min,
        protocol_max=protocol_max,
        entries=tuple(entries),
    )


def _write_source_tar(path: Path, snapshot: GitSnapshot) -> None:
    prefix = f"remote-mcp-commander-{snapshot.package_version}"
    with path.open("wb") as handle:
        with tarfile.open(fileobj=handle, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for entry in snapshot.entries:
                name = f"{prefix}/{entry.path}"
                if len(name.encode("utf-8")) > MAX_USTAR_NAME_BYTES:
                    raise PackagingError(f"source path exceeds USTAR limit: {entry.path}")
                info = tarfile.TarInfo(name=name)
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mtime = snapshot.source_date_epoch
                info.mode = entry.mode
                if entry.kind == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = entry.data.decode("utf-8")
                    if len(info.linkname.encode("utf-8")) > MAX_USTAR_LINK_BYTES:
                        raise PackagingError(f"symlink target exceeds USTAR limit: {entry.path}")
                    info.size = 0
                    archive.addfile(info)
                else:
                    info.type = tarfile.REGTYPE
                    info.size = len(entry.data)
                    import io

                    archive.addfile(info, io.BytesIO(entry.data))


def _materialize_source(root: Path, snapshot: GitSnapshot) -> None:
    root.mkdir(parents=True, exist_ok=False)
    for entry in snapshot.entries:
        path = root.joinpath(*PurePosixPath(entry.path).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        if entry.kind == "symlink":
            os.symlink(entry.data.decode("utf-8"), path)
        else:
            path.write_bytes(entry.data)
            path.chmod(entry.mode)


def _sha256_file(path: Path) -> tuple[str, int]:
    flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PackagingError(f"cannot open artifact safely: {path.name}") from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise PackagingError(f"artifact is not a regular file: {path.name}")
        if before.st_size < 1 or before.st_size > MAX_ARTIFACT_BYTES:
            raise PackagingError(f"artifact size is outside allowed bounds: {path.name}")
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(fd)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after:
            raise PackagingError(f"artifact changed while being read: {path.name}")
        return digest.hexdigest(), before.st_size
    finally:
        os.close(fd)


def _read_small_regular(path: Path, *, limit: int, label: str) -> bytes:
    flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PackagingError(f"cannot open {label} safely") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise PackagingError(f"{label} must be a regular file")
        if before.st_size < 1 or before.st_size > limit:
            raise PackagingError(f"{label} size is outside allowed bounds")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                raise PackagingError(f"{label} was truncated while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after:
            raise PackagingError(f"{label} changed while being read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _builder_metadata(builder_python: str) -> tuple[str, dict[str, str]]:
    script = (
        "import importlib.metadata as m,json,sys;"
        "print(json.dumps({'python':f'{sys.version_info.major}.{sys.version_info.minor}',"
        "'build':m.version('build'),'setuptools':m.version('setuptools'),'wheel':m.version('wheel')},sort_keys=True))"
    )
    try:
        payload = json.loads(_run([builder_python, "-c", script]).decode("utf-8"))
    except (json.JSONDecodeError, KeyError) as exc:
        raise PackagingError("cannot read builder toolchain metadata") from exc
    python_minor = str(payload.pop("python", ""))
    toolchain = {str(key): str(value) for key, value in payload.items()}
    if python_minor != BUILDER_PYTHON_MINOR:
        raise PackagingError(
            f"builder Python must be {BUILDER_PYTHON_MINOR}; got {python_minor or 'unknown'}"
        )
    if toolchain != BUILD_TOOLCHAIN:
        raise PackagingError("builder toolchain versions do not match the package contract")
    return python_minor, toolchain


def _build_wheel(
    source_root: Path,
    output_dir: Path,
    *,
    builder_python: str,
    source_date_epoch: int,
    home: Path,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=False)
    home.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "SOURCE_DATE_EPOCH": str(source_date_epoch),
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TZ": "UTC",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    _run(
        [
            builder_python,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(output_dir),
            str(source_root),
        ],
        env=env,
    )
    wheels = sorted(output_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise PackagingError("wheel build must produce exactly one wheel")
    return wheels[0]


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _write_manifest(path: Path, manifest: PackageManifest) -> None:
    path.write_bytes(_canonical_json_bytes(manifest.model_dump(mode="json")))


def build_bundle(
    *,
    repo: Path,
    commit: str,
    output: Path,
    builder_python: str = sys.executable,
) -> PackageManifest:
    repo = _validate_repo(repo)
    snapshot = _snapshot(repo, commit)
    python_minor, toolchain = _builder_metadata(builder_python)
    output = output.expanduser()
    try:
        parent = output.parent.resolve(strict=True)
    except OSError as exc:
        raise PackagingError("package output parent directory does not exist") from exc
    if not parent.is_dir():
        raise PackagingError("package output parent must be a directory")
    output = parent / output.name
    if output.exists() or output.is_symlink():
        raise PackagingError("package output path already exists")
    temp = parent / f".{output.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    try:
        temp.mkdir(mode=0o700)
        with tempfile.TemporaryDirectory(prefix="rmc-package-") as raw_work:
            work = Path(raw_work)
            source_a = work / "source-a.tar"
            source_b = work / "source-b.tar"
            _write_source_tar(source_a, snapshot)
            _write_source_tar(source_b, snapshot)
            source_hash_a, source_size_a = _sha256_file(source_a)
            source_hash_b, source_size_b = _sha256_file(source_b)
            if (source_hash_a, source_size_a) != (source_hash_b, source_size_b):
                raise PackagingError("source archive reproducibility check failed")

            tree_a = work / "tree-a"
            tree_b = work / "tree-b"
            _materialize_source(tree_a, snapshot)
            _materialize_source(tree_b, snapshot)
            wheel_a = _build_wheel(
                tree_a,
                work / "wheel-a",
                builder_python=builder_python,
                source_date_epoch=snapshot.source_date_epoch,
                home=work / "home-a",
            )
            wheel_b = _build_wheel(
                tree_b,
                work / "wheel-b",
                builder_python=builder_python,
                source_date_epoch=snapshot.source_date_epoch,
                home=work / "home-b",
            )
            wheel_hash_a, wheel_size_a = _sha256_file(wheel_a)
            wheel_hash_b, wheel_size_b = _sha256_file(wheel_b)
            if wheel_a.name != wheel_b.name or (wheel_hash_a, wheel_size_a) != (
                wheel_hash_b,
                wheel_size_b,
            ):
                raise PackagingError("wheel reproducibility check failed")

            short = snapshot.commit_sha[:12]
            source_name = f"remote-mcp-commander-{snapshot.package_version}-{short}.source.tar"
            source_dest = temp / source_name
            wheel_dest = temp / wheel_a.name
            shutil.copyfile(source_a, source_dest)
            shutil.copyfile(wheel_a, wheel_dest)
            source_record = ArtifactRecord(
                kind="source", filename=source_name, sha256=source_hash_a, size=source_size_a
            )
            wheel_record = ArtifactRecord(
                kind="wheel", filename=wheel_a.name, sha256=wheel_hash_a, size=wheel_size_a
            )
            manifest = PackageManifest(
                commit_sha=snapshot.commit_sha,
                git_tree_sha=snapshot.tree_sha,
                source_date_epoch=snapshot.source_date_epoch,
                package_version=snapshot.package_version,
                protocol_min=snapshot.protocol_min,
                protocol_max=snapshot.protocol_max,
                builder_python=python_minor,
                toolchain=toolchain,
                artifacts=[source_record, wheel_record],
            )
            _write_manifest(temp / PACKAGE_MANIFEST, manifest)
            verify_bundle(temp)
        os.replace(temp, output)
        return manifest
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PackagingError(f"duplicate package manifest key: {key}")
        result[key] = value
    return result


def load_package_manifest(bundle: Path) -> PackageManifest:
    path = bundle / PACKAGE_MANIFEST
    try:
        raw = _read_small_regular(path, limit=MAX_MANIFEST_BYTES, label="package manifest")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        return PackageManifest.model_validate(value)
    except PackagingError:
        raise
    except Exception as exc:
        raise PackagingError("package manifest is invalid") from exc


def _verify_source_artifact(path: Path, record: ArtifactRecord, manifest: PackageManifest) -> None:
    flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PackagingError("cannot open source artifact safely") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size != record.size:
            raise PackagingError("source artifact size/type does not match manifest")
        if before.st_size < 1 or before.st_size > MAX_ARTIFACT_BYTES:
            raise PackagingError("source artifact size is outside allowed bounds")

        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        if digest.hexdigest() != record.sha256:
            raise PackagingError("source artifact does not match manifest")

        os.lseek(fd, 0, os.SEEK_SET)
        prefix = f"remote-mcp-commander-{manifest.package_version}/"
        total_payload = 0
        try:
            with os.fdopen(os.dup(fd), "rb") as handle:
                with tarfile.open(fileobj=handle, mode="r:") as archive:
                    members = archive.getmembers()
        except (OSError, tarfile.TarError, ValueError) as exc:
            raise PackagingError("source archive is invalid") from exc
        if not members or len(members) > MAX_SOURCE_ENTRIES:
            raise PackagingError("source archive entry count is invalid")
        last_name = ""
        for member in members:
            if not member.name.startswith(prefix):
                raise PackagingError("source archive prefix does not match package version")
            relative = member.name[len(prefix) :]
            pure = PurePosixPath(relative)
            if (
                pure.is_absolute()
                or not pure.parts
                or any(part in {"", ".", ".."} for part in pure.parts)
            ):
                raise PackagingError("source archive contains an unsafe path")
            if member.name <= last_name:
                raise PackagingError("source archive entries are not strictly sorted")
            last_name = member.name
            if member.uid != 0 or member.gid != 0 or member.uname or member.gname:
                raise PackagingError("source archive ownership metadata is not normalized")
            if member.mtime != manifest.source_date_epoch:
                raise PackagingError("source archive timestamp does not match SOURCE_DATE_EPOCH")
            if member.isreg():
                total_payload += member.size
                if total_payload > MAX_SOURCE_BYTES:
                    raise PackagingError("source archive payload exceeds the package byte limit")
                if stat.S_IMODE(member.mode) not in {0o644, 0o755}:
                    raise PackagingError("source archive contains an unexpected file mode")
            elif member.issym():
                target = PurePosixPath(member.linkname)
                if target.is_absolute() or ".." in target.parts:
                    raise PackagingError("source archive contains an external symlink")
            else:
                raise PackagingError("source archive contains an unsupported entry type")

        os.lseek(fd, 0, os.SEEK_SET)
        digest_after = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest_after.update(chunk)
        after = os.fstat(fd)
        if digest_after.hexdigest() != record.sha256:
            raise PackagingError("source artifact changed during verification")
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after:
            raise PackagingError("source artifact changed during verification")
    finally:
        os.close(fd)


def verify_bundle(bundle: Path) -> PackageManifest:
    bundle = bundle.expanduser()
    if bundle.is_symlink():
        raise PackagingError("package bundle must not be a symlink")
    try:
        bundle = bundle.resolve(strict=True)
    except OSError as exc:
        raise PackagingError("package bundle does not exist") from exc
    if not bundle.is_dir():
        raise PackagingError("package bundle must be a directory")
    manifest = load_package_manifest(bundle)
    expected = {PACKAGE_MANIFEST, *(item.filename for item in manifest.artifacts)}
    actual = {item.name for item in bundle.iterdir()}
    allowed_sets = (expected, expected | {PACKAGE_SIGNATURE})
    if actual not in allowed_sets:
        raise PackagingError("package bundle file set does not match the manifest")
    for record in manifest.artifacts:
        path = bundle / record.filename
        if record.kind == "source":
            _verify_source_artifact(path, record, manifest)
            continue
        digest, size = _sha256_file(path)
        if digest != record.sha256 or size != record.size:
            raise PackagingError(f"package artifact does not match manifest: {record.filename}")
    if {item.name for item in bundle.iterdir()} not in allowed_sets:
        raise PackagingError("package bundle changed during verification")
    return manifest


def _print_manifest(manifest: PackageManifest, *, json_output: bool) -> None:
    payload = manifest.model_dump(mode="json")
    if json_output:
        print(json.dumps(payload, sort_keys=True))
        return
    print(f"commit: {manifest.commit_sha}")
    print(f"tree: {manifest.git_tree_sha}")
    print(f"version: {manifest.package_version}")
    print(f"source_date_epoch: {manifest.source_date_epoch}")
    for artifact in manifest.artifacts:
        print(
            f"{artifact.kind}: {artifact.filename} sha256={artifact.sha256} bytes={artifact.size}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build, sign, and verify Remote MCP Commander artifacts"
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--repo", default=".")
    build_parser.add_argument("--commit", default="HEAD")
    build_parser.add_argument("--output", required=True)
    build_parser.add_argument("--builder-python", default=sys.executable)
    build_parser.add_argument("--json", action="store_true", dest="json_output")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--bundle", required=True)
    verify_parser.add_argument("--json", action="store_true", dest="json_output")
    sign_parser = subparsers.add_parser("sign")
    sign_parser.add_argument("--bundle", required=True)
    sign_parser.add_argument("--key-id", required=True)
    sign_parser.add_argument("--private-key", required=True)
    sign_parser.add_argument("--json", action="store_true", dest="json_output")
    trusted_parser = subparsers.add_parser("verify-trusted")
    trusted_parser.add_argument("--bundle", required=True)
    trusted_parser.add_argument("--trusted-keys-dir", required=True)
    trusted_parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)

    from remote_mcp_commander.package_signing import (
        PackageSigningError,
        sign_package_bundle,
        verify_signed_package_bundle,
    )

    try:
        if args.action == "build":
            manifest = build_bundle(
                repo=Path(args.repo),
                commit=args.commit,
                output=Path(args.output),
                builder_python=args.builder_python,
            )
            _print_manifest(manifest, json_output=args.json_output)
            return 0
        if args.action == "verify":
            manifest = verify_bundle(Path(args.bundle))
            _print_manifest(manifest, json_output=args.json_output)
            return 0
        if args.action == "sign":
            signature = sign_package_bundle(
                Path(args.bundle),
                key_id=args.key_id,
                private_key_path=Path(args.private_key),
            )
            payload = signature.model_dump(mode="json")
            if args.json_output:
                print(json.dumps(payload, sort_keys=True))
            else:
                print(f"signed package manifest with key: {signature.key_id}")
                print(f"manifest_sha256: {signature.manifest_sha256}")
            return 0

        manifest, signature = verify_signed_package_bundle(
            Path(args.bundle),
            trusted_keys_dir=Path(args.trusted_keys_dir),
        )
        if args.json_output:
            print(
                json.dumps(
                    {
                        "manifest": manifest.model_dump(mode="json"),
                        "signature": signature.model_dump(mode="json"),
                    },
                    sort_keys=True,
                )
            )
        else:
            print(f"trusted package: {manifest.commit_sha}")
            print(f"signing_key: {signature.key_id}")
        return 0
    except (PackagingError, PackageSigningError) as exc:
        if getattr(args, "json_output", False):
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        else:
            print(f"package error: {exc}")
        return 1


def run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    run()

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import stat
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

MANIFEST_FILENAME = "release-manifest.json"
MANIFEST_SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_ENTRIES = 50_000
MAX_RELEASE_BYTES = 8 * 1024 * 1024 * 1024
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
VENV_PYTHON_LINK_RE = re.compile(r"^\.venv/bin/python(?:3(?:\.\d+)?)?$")
HEX_SHA256_RE = r"^[0-9a-f]{64}$"


class IntegrityError(RuntimeError):
    pass


class ManifestEntry(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    kind: Literal["directory", "file", "symlink"]
    mode: int = Field(ge=0, le=0o7777)
    size: int | None = Field(default=None, ge=0)
    sha256: str | None = Field(default=None, pattern=HEX_SHA256_RE)
    target: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def validate_shape(self) -> ManifestEntry:
        path = Path(self.path)
        if (
            "\x00" in self.path
            or path.is_absolute()
            or ".." in path.parts
            or self.path == MANIFEST_FILENAME
        ):
            raise ValueError("manifest entry path must stay inside the release")
        if self.kind == "file":
            if self.size is None or self.sha256 is None or self.target is not None:
                raise ValueError("file entries require size/hash and no symlink target")
        elif self.kind == "symlink":
            if self.target is None or self.size is not None or self.sha256 is not None:
                raise ValueError("symlink entries require a target only")
        elif self.size is not None or self.sha256 is not None or self.target is not None:
            raise ValueError("directory entries cannot carry file/symlink fields")
        return self


class ReleaseManifest(BaseModel):
    schema_version: Literal[1] = MANIFEST_SCHEMA_VERSION
    release_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    package_version: str = Field(min_length=1, max_length=128)
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
    protocol_min: int = Field(ge=1, le=65_535)
    protocol_max: int = Field(ge=1, le=65_535)
    created_at: datetime
    total_bytes: int = Field(ge=0, le=MAX_RELEASE_BYTES)
    tree_sha256: str = Field(pattern=HEX_SHA256_RE)
    entries: list[ManifestEntry] = Field(max_length=MAX_MANIFEST_ENTRIES)

    @model_validator(mode="after")
    def validate_manifest(self) -> ReleaseManifest:
        if self.protocol_min > self.protocol_max:
            raise ValueError("protocol_min must be <= protocol_max")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("manifest created_at must be timezone-aware")
        paths = [entry.path for entry in self.entries]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("manifest entries must be unique and sorted")
        total = sum(entry.size or 0 for entry in self.entries)
        if total != self.total_bytes:
            raise ValueError("manifest total_bytes does not match entries")
        if _tree_digest(self.entries) != self.tree_sha256:
            raise ValueError("manifest tree digest does not match entries")
        return self


def _tree_digest(entries: list[ManifestEntry]) -> str:
    payload = [entry.model_dump(mode="json", exclude_none=True) for entry in entries]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _literal_assignment(path: Path, name: str) -> object:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise IntegrityError(f"cannot parse release metadata: {path.name}") from exc
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                return ast.literal_eval(node.value)
        elif isinstance(node, ast.AnnAssign):
            if (
                isinstance(node.target, ast.Name)
                and node.target.id == name
                and node.value is not None
            ):
                return ast.literal_eval(node.value)
    raise IntegrityError(f"missing release metadata assignment: {name}")


def _candidate_metadata(release_dir: Path) -> tuple[str, int, int]:
    pyproject = release_dir / "pyproject.toml"
    try:
        project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
        package_version = str(project["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise IntegrityError("release is missing valid project version metadata") from exc

    package_init = release_dir / "src" / "remote_mcp_commander" / "__init__.py"
    protocol_module = release_dir / "src" / "remote_mcp_commander" / "protocol.py"
    source_version = _literal_assignment(package_init, "__version__")
    protocol_min = _literal_assignment(protocol_module, "PROTOCOL_MIN_SUPPORTED")
    protocol_max = _literal_assignment(protocol_module, "PROTOCOL_MAX_SUPPORTED")
    if source_version != package_version:
        raise IntegrityError("package version metadata is inconsistent")
    if not isinstance(protocol_min, int) or not isinstance(protocol_max, int):
        raise IntegrityError("protocol metadata must be integers")
    if protocol_min < 1 or protocol_max < protocol_min or protocol_max > 65_535:
        raise IntegrityError("release protocol metadata is invalid")
    return package_version, protocol_min, protocol_max


def _validate_symlink_target(release_dir: Path, path: Path, relative: str, target: str) -> None:
    if "\x00" in target:
        raise IntegrityError(f"invalid symlink target in release: {relative}")
    target_path = Path(target) if Path(target).is_absolute() else path.parent / target
    try:
        resolved_target = target_path.resolve(strict=True)
        resolved_root = release_dir.resolve(strict=True)
    except OSError as exc:
        raise IntegrityError(f"dangling symlink in release: {relative}") from exc
    if resolved_target.is_relative_to(resolved_root):
        return
    if VENV_PYTHON_LINK_RE.fullmatch(relative):
        return
    raise IntegrityError(f"external symlink is outside release integrity boundary: {relative}")


def _hash_regular_file(path: Path, expected_size: int) -> str:
    flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise IntegrityError(f"cannot open release file safely: {path.name}") from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            raise IntegrityError("release file changed during integrity scan")
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(fd)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after:
            raise IntegrityError("release file changed during integrity scan")
    finally:
        os.close(fd)
    return digest.hexdigest()


def _collect_entries(release_dir: Path) -> tuple[list[ManifestEntry], int]:
    entries: list[ManifestEntry] = []
    total_bytes = 0
    stack = [release_dir]
    while stack:
        directory = stack.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name, reverse=True)
        except OSError as exc:
            raise IntegrityError("cannot enumerate release tree") from exc
        for child in children:
            path = Path(child.path)
            relative = path.relative_to(release_dir).as_posix()
            if relative == MANIFEST_FILENAME:
                continue
            if ".git" in Path(relative).parts:
                raise IntegrityError("release artifacts must not contain .git metadata")
            if len(entries) >= MAX_MANIFEST_ENTRIES:
                raise IntegrityError("release contains too many manifest entries")
            try:
                info = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise IntegrityError(f"cannot inspect release entry: {relative}") from exc
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                try:
                    target = os.readlink(path)
                except OSError as exc:
                    raise IntegrityError(f"cannot read release symlink: {relative}") from exc
                _validate_symlink_target(release_dir, path, relative, target)
                entries.append(
                    ManifestEntry(path=relative, kind="symlink", mode=mode, target=target)
                )
            elif stat.S_ISDIR(info.st_mode):
                entries.append(ManifestEntry(path=relative, kind="directory", mode=mode))
                stack.append(path)
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise IntegrityError(f"hard-linked file is not allowed in release: {relative}")
                total_bytes += info.st_size
                if total_bytes > MAX_RELEASE_BYTES:
                    raise IntegrityError("release exceeds the integrity scan byte limit")
                entries.append(
                    ManifestEntry(
                        path=relative,
                        kind="file",
                        mode=mode,
                        size=info.st_size,
                        sha256=_hash_regular_file(path, info.st_size),
                    )
                )
            else:
                raise IntegrityError(f"unsupported special file in release: {relative}")
    entries.sort(key=lambda entry: entry.path)
    return entries, total_bytes


def _manifest_path(release_dir: Path) -> Path:
    return release_dir / MANIFEST_FILENAME


def load_manifest(release_dir: Path) -> ReleaseManifest:
    path = _manifest_path(release_dir)
    if path.is_symlink() or not path.is_file():
        raise IntegrityError("release manifest is missing or is not a regular file")
    try:
        manifest_stat = path.stat()
        if os.name != "nt" and stat.S_IMODE(manifest_stat.st_mode) & 0o222:
            raise IntegrityError("release manifest must be read-only")
        if manifest_stat.st_size > MAX_MANIFEST_BYTES:
            raise IntegrityError("release manifest exceeds the size limit")

        def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise IntegrityError("release manifest contains duplicate JSON keys")
                result[key] = value
            return result

        payload = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys
        )
        return ReleaseManifest.model_validate(payload)
    except IntegrityError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise IntegrityError("release manifest is invalid") from exc


def seal_release(release_dir: Path, *, commit_sha: str) -> ReleaseManifest:
    commit_sha = commit_sha.lower()
    if not COMMIT_SHA_RE.fullmatch(commit_sha):
        raise IntegrityError("commit SHA must be 40 or 64 hexadecimal characters")
    manifest_path = _manifest_path(release_dir)
    if manifest_path.exists() or manifest_path.is_symlink():
        raise IntegrityError("release manifest already exists")
    package_version, protocol_min, protocol_max = _candidate_metadata(release_dir)
    entries, total_bytes = _collect_entries(release_dir)
    manifest = ReleaseManifest(
        release_id=release_dir.name,
        package_version=package_version,
        commit_sha=commit_sha,
        protocol_min=protocol_min,
        protocol_max=protocol_max,
        created_at=datetime.now(UTC),
        total_bytes=total_bytes,
        tree_sha256=_tree_digest(entries),
        entries=entries,
    )
    data = (
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
    )
    if len(data) > MAX_MANIFEST_BYTES:
        raise IntegrityError("generated release manifest exceeds the size limit")
    temp = release_dir / f".{MANIFEST_FILENAME}.tmp.{os.getpid()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(temp, flags, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, manifest_path)
        if os.name != "nt":
            os.chmod(manifest_path, 0o444)
    finally:
        if temp.exists() or temp.is_symlink():
            temp.unlink()
    return verify_release(release_dir)


def verify_release(release_dir: Path) -> ReleaseManifest:
    manifest = load_manifest(release_dir)
    if manifest.release_id != release_dir.name:
        raise IntegrityError("release manifest ID does not match directory name")
    package_version, protocol_min, protocol_max = _candidate_metadata(release_dir)
    if package_version != manifest.package_version:
        raise IntegrityError("release package version does not match manifest")
    if (protocol_min, protocol_max) != (manifest.protocol_min, manifest.protocol_max):
        raise IntegrityError("release protocol range does not match manifest")
    entries, total_bytes = _collect_entries(release_dir)
    if total_bytes != manifest.total_bytes or entries != manifest.entries:
        raise IntegrityError("release tree does not match manifest")
    if _tree_digest(entries) != manifest.tree_sha256:
        raise IntegrityError("release tree digest does not match manifest")
    return manifest

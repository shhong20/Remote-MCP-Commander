from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, Field

from remote_mcp_commander.release_integrity import (
    MANIFEST_FILENAME,
    SIGNATURE_FILENAME,
    ReleaseManifest,
    verify_release,
)

SIGNATURE_SCHEMA_VERSION = 1
MAX_SIGNATURE_BYTES = 16 * 1024
MAX_KEY_BYTES = 16 * 1024
KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SigningError(RuntimeError):
    pass


class ReleaseSignature(BaseModel):
    schema_version: Literal[1] = SIGNATURE_SCHEMA_VERSION
    algorithm: Literal["ed25519"] = "ed25519"
    key_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature: str = Field(min_length=1, max_length=256)


def _safe_file_bytes(
    path: Path,
    *,
    limit: int,
    label: str,
    forbidden_mode_mask: int = 0,
) -> bytes:
    if path.is_symlink():
        raise SigningError(f"{label} must be a regular non-symlink file")
    flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SigningError(f"cannot open {label} safely") from exc
    chunks: list[bytes] = []
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise SigningError(f"{label} must be a regular non-symlink file")
        if before.st_size > limit:
            raise SigningError(f"{label} exceeds the size limit")
        if os.name != "nt" and stat.S_IMODE(before.st_mode) & forbidden_mode_mask:
            raise SigningError(f"{label} has unsafe file permissions")
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                raise SigningError(f"{label} changed while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after:
            raise SigningError(f"{label} changed while being read")
    finally:
        os.close(fd)
    return b"".join(chunks)


def _manifest_bytes(release_dir: Path) -> bytes:
    return _safe_file_bytes(
        release_dir / MANIFEST_FILENAME,
        limit=16 * 1024 * 1024,
        label="release manifest",
        forbidden_mode_mask=0o222,
    )


def _signature_path(release_dir: Path) -> Path:
    return release_dir / SIGNATURE_FILENAME


def _write_readonly_json(path: Path, payload: dict[str, object]) -> None:
    if path.exists() or path.is_symlink():
        raise SigningError("release signature already exists")
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if len(data) > MAX_SIGNATURE_BYTES:
        raise SigningError("release signature exceeds the size limit")
    temp = path.parent / f".{path.name}.tmp.{os.getpid()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(temp, flags, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, path)
        if os.name != "nt":
            os.chmod(path, 0o444)
    finally:
        if temp.exists() or temp.is_symlink():
            temp.unlink()


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    data = _safe_file_bytes(
        path,
        limit=MAX_KEY_BYTES,
        label="private signing key",
        forbidden_mode_mask=0o077,
    )
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (TypeError, ValueError) as exc:
        raise SigningError("private signing key is not a valid unencrypted PEM key") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise SigningError("private signing key must be Ed25519")
    return key


def _trusted_key_path(trusted_keys_dir: Path, key_id: str) -> Path:
    if not KEY_ID_RE.fullmatch(key_id):
        raise SigningError("invalid release signing key ID")
    if trusted_keys_dir.is_symlink() or not trusted_keys_dir.is_dir():
        raise SigningError("trusted release key directory is missing or unsafe")
    if os.name != "nt" and stat.S_IMODE(trusted_keys_dir.stat().st_mode) & 0o022:
        raise SigningError("trusted release key directory must not be group/world writable")
    return trusted_keys_dir / f"{key_id}.pem"


def _load_public_key(trusted_keys_dir: Path, key_id: str) -> Ed25519PublicKey:
    path = _trusted_key_path(trusted_keys_dir, key_id)
    data = _safe_file_bytes(
        path,
        limit=MAX_KEY_BYTES,
        label="trusted release public key",
        forbidden_mode_mask=0o022,
    )
    try:
        key = serialization.load_pem_public_key(data)
    except ValueError as exc:
        raise SigningError("trusted release public key is invalid") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise SigningError("trusted release public key must be Ed25519")
    return key


def load_signature(release_dir: Path) -> ReleaseSignature:
    path = _signature_path(release_dir)
    data = _safe_file_bytes(
        path,
        limit=MAX_SIGNATURE_BYTES,
        label="release signature",
        forbidden_mode_mask=0o222,
    )

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise SigningError("release signature contains duplicate JSON keys")
            result[key] = value
        return result

    try:
        payload = json.loads(data, object_pairs_hook=reject_duplicates)
        result = ReleaseSignature.model_validate(payload)
        raw_signature = base64.b64decode(result.signature, validate=True)
    except SigningError:
        raise
    except (ValueError, json.JSONDecodeError) as exc:
        raise SigningError("release signature is invalid") from exc
    if len(raw_signature) != 64:
        raise SigningError("Ed25519 signature has an invalid length")
    return result


def sign_release(
    release_dir: Path,
    *,
    key_id: str,
    private_key_path: Path,
) -> ReleaseSignature:
    if not KEY_ID_RE.fullmatch(key_id):
        raise SigningError("invalid release signing key ID")
    private_input = private_key_path.expanduser()
    if private_input.is_symlink():
        raise SigningError("private signing key must not be a symlink")
    try:
        resolved_release = release_dir.resolve(strict=True)
        resolved_private = private_input.resolve(strict=True)
    except OSError as exc:
        raise SigningError("private signing key path is invalid") from exc
    if resolved_private.is_relative_to(resolved_release):
        raise SigningError("private signing key must be outside the release directory")
    verify_release(release_dir)
    key = _load_private_key(resolved_private)
    manifest = _manifest_bytes(release_dir)
    result = ReleaseSignature(
        key_id=key_id,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        signature=base64.b64encode(key.sign(manifest)).decode("ascii"),
    )
    _write_readonly_json(_signature_path(release_dir), result.model_dump(mode="json"))
    return load_signature(release_dir)


def verify_signed_release(
    release_dir: Path,
    *,
    trusted_keys_dir: Path,
) -> tuple[ReleaseManifest, ReleaseSignature]:
    trust_input = trusted_keys_dir.expanduser()
    if trust_input.is_symlink():
        raise SigningError("trusted release key directory must not be a symlink")
    try:
        resolved_release = release_dir.resolve(strict=True)
        resolved_trust = trust_input.resolve(strict=True)
    except OSError as exc:
        raise SigningError("trusted release key directory is missing or unsafe") from exc
    if resolved_trust.is_relative_to(resolved_release):
        raise SigningError("trusted release keys must be outside the release directory")
    signature = load_signature(release_dir)
    manifest_bytes = _manifest_bytes(release_dir)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if manifest_sha256 != signature.manifest_sha256:
        raise SigningError("release signature references a different manifest")
    public_key = _load_public_key(resolved_trust, signature.key_id)
    try:
        raw_signature = base64.b64decode(signature.signature, validate=True)
        public_key.verify(raw_signature, manifest_bytes)
    except (InvalidSignature, ValueError) as exc:
        raise SigningError("release manifest signature verification failed") from exc

    manifest = verify_release(release_dir)
    if hashlib.sha256(_manifest_bytes(release_dir)).hexdigest() != manifest_sha256:
        raise SigningError("release manifest changed during signed verification")
    if load_signature(release_dir) != signature:
        raise SigningError("release signature changed during signed verification")
    return manifest, signature

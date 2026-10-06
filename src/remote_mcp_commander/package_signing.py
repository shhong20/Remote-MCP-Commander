from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field

from remote_mcp_commander.package import (
    MAX_MANIFEST_BYTES,
    PACKAGE_MANIFEST,
    PACKAGE_SIGNATURE,
    PackageManifest,
    _read_small_regular,
    verify_bundle,
)

SIGNATURE_SCHEMA_VERSION = 1
SIGNATURE_DOMAIN = b"remote-mcp-commander/package-manifest/v1\x00"
SIGNATURE_DOMAIN_NAME = "remote-mcp-commander/package-manifest/v1"
MAX_SIGNATURE_BYTES = 16 * 1024
MAX_KEY_BYTES = 16 * 1024
KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class PackageSigningError(RuntimeError):
    pass


class PackageSignature(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = SIGNATURE_SCHEMA_VERSION
    algorithm: Literal["ed25519"] = "ed25519"
    domain: Literal["remote-mcp-commander/package-manifest/v1"] = SIGNATURE_DOMAIN_NAME
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
        raise PackageSigningError(f"{label} must be a regular non-symlink file")
    flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PackageSigningError(f"cannot open {label} safely") from exc
    chunks: list[bytes] = []
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise PackageSigningError(f"{label} must be a regular non-symlink file")
        if before.st_size < 1 or before.st_size > limit:
            raise PackageSigningError(f"{label} size is outside allowed bounds")
        if os.name != "nt" and stat.S_IMODE(before.st_mode) & forbidden_mode_mask:
            raise PackageSigningError(f"{label} has unsafe file permissions")
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                raise PackageSigningError(f"{label} changed while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after:
            raise PackageSigningError(f"{label} changed while being read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _manifest_bytes(bundle: Path) -> bytes:
    try:
        return _read_small_regular(
            bundle / PACKAGE_MANIFEST, limit=MAX_MANIFEST_BYTES, label="package manifest"
        )
    except Exception as exc:
        raise PackageSigningError("cannot read package manifest safely") from exc


def _write_readonly_json(path: Path, payload: dict[str, object]) -> None:
    if path.exists() or path.is_symlink():
        raise PackageSigningError("package signature already exists")
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if len(data) > MAX_SIGNATURE_BYTES:
        raise PackageSigningError("package signature exceeds the size limit")
    temp = path.parent / f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
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
        label="package private signing key",
        forbidden_mode_mask=0o077,
    )
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (TypeError, ValueError) as exc:
        raise PackageSigningError("package private key is not a valid unencrypted PEM") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise PackageSigningError("package private signing key must be Ed25519")
    return key


def _trusted_key_path(trusted_keys_dir: Path, key_id: str) -> Path:
    if not KEY_ID_RE.fullmatch(key_id):
        raise PackageSigningError("invalid package signing key ID")
    if trusted_keys_dir.is_symlink() or not trusted_keys_dir.is_dir():
        raise PackageSigningError("trusted package key directory is missing or unsafe")
    if os.name != "nt" and stat.S_IMODE(trusted_keys_dir.stat().st_mode) & 0o022:
        raise PackageSigningError("trusted package key directory must not be group/world writable")
    return trusted_keys_dir / f"{key_id}.pem"


def _load_public_key(trusted_keys_dir: Path, key_id: str) -> Ed25519PublicKey:
    path = _trusted_key_path(trusted_keys_dir, key_id)
    data = _safe_file_bytes(
        path,
        limit=MAX_KEY_BYTES,
        label="trusted package public key",
        forbidden_mode_mask=0o022,
    )
    try:
        key = serialization.load_pem_public_key(data)
    except ValueError as exc:
        raise PackageSigningError("trusted package public key is invalid") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise PackageSigningError("trusted package public key must be Ed25519")
    return key


def load_package_signature(bundle: Path) -> PackageSignature:
    data = _safe_file_bytes(
        bundle / PACKAGE_SIGNATURE,
        limit=MAX_SIGNATURE_BYTES,
        label="package signature",
        forbidden_mode_mask=0o222,
    )

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise PackageSigningError("package signature contains duplicate JSON keys")
            result[key] = value
        return result

    try:
        payload = json.loads(data, object_pairs_hook=reject_duplicates)
        result = PackageSignature.model_validate(payload)
        raw_signature = base64.b64decode(result.signature, validate=True)
    except PackageSigningError:
        raise
    except (ValueError, json.JSONDecodeError) as exc:
        raise PackageSigningError("package signature is invalid") from exc
    if len(raw_signature) != 64:
        raise PackageSigningError("Ed25519 package signature has an invalid length")
    return result


def _resolve_bundle(bundle: Path) -> Path:
    bundle_input = bundle.expanduser()
    if bundle_input.is_symlink():
        raise PackageSigningError("package bundle must not be a symlink")
    try:
        resolved = bundle_input.resolve(strict=True)
    except OSError as exc:
        raise PackageSigningError("package bundle path is invalid") from exc
    if not resolved.is_dir():
        raise PackageSigningError("package bundle must be a directory")
    return resolved


def sign_package_bundle(
    bundle: Path,
    *,
    key_id: str,
    private_key_path: Path,
) -> PackageSignature:
    if not KEY_ID_RE.fullmatch(key_id):
        raise PackageSigningError("invalid package signing key ID")
    resolved_bundle = _resolve_bundle(bundle)
    private_input = private_key_path.expanduser()
    if private_input.is_symlink():
        raise PackageSigningError("package private signing key must not be a symlink")
    try:
        resolved_private = private_input.resolve(strict=True)
    except OSError as exc:
        raise PackageSigningError("package private signing key path is invalid") from exc
    if resolved_private.is_relative_to(resolved_bundle):
        raise PackageSigningError("package private signing key must be outside the bundle")

    verify_bundle(resolved_bundle)
    key = _load_private_key(resolved_private)
    manifest_bytes = _manifest_bytes(resolved_bundle)
    signed_payload = SIGNATURE_DOMAIN + manifest_bytes
    result = PackageSignature(
        key_id=key_id,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        signature=base64.b64encode(key.sign(signed_payload)).decode("ascii"),
    )
    _write_readonly_json(
        resolved_bundle / PACKAGE_SIGNATURE,
        result.model_dump(mode="json"),
    )
    return load_package_signature(resolved_bundle)


def verify_signed_package_bundle(
    bundle: Path,
    *,
    trusted_keys_dir: Path,
) -> tuple[PackageManifest, PackageSignature]:
    resolved_bundle = _resolve_bundle(bundle)
    trust_input = trusted_keys_dir.expanduser()
    if trust_input.is_symlink():
        raise PackageSigningError("trusted package key directory must not be a symlink")
    try:
        resolved_trust = trust_input.resolve(strict=True)
    except OSError as exc:
        raise PackageSigningError("trusted package key directory is missing or unsafe") from exc
    if resolved_trust.is_relative_to(resolved_bundle):
        raise PackageSigningError("trusted package keys must be outside the bundle")

    signature = load_package_signature(resolved_bundle)
    manifest_bytes = _manifest_bytes(resolved_bundle)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if manifest_sha256 != signature.manifest_sha256:
        raise PackageSigningError("package signature references a different manifest")
    public_key = _load_public_key(resolved_trust, signature.key_id)
    try:
        raw_signature = base64.b64decode(signature.signature, validate=True)
        public_key.verify(raw_signature, SIGNATURE_DOMAIN + manifest_bytes)
    except (InvalidSignature, ValueError) as exc:
        raise PackageSigningError("package manifest signature verification failed") from exc

    manifest = verify_bundle(resolved_bundle)
    if hashlib.sha256(_manifest_bytes(resolved_bundle)).hexdigest() != manifest_sha256:
        raise PackageSigningError("package manifest changed during signed verification")
    if load_package_signature(resolved_bundle) != signature:
        raise PackageSigningError("package signature changed during signed verification")
    return manifest, signature

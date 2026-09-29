from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field

from remote_mcp_commander.package import PackagingError, _read_small_regular
from remote_mcp_commander.runtime_lock import (
    MAX_LOCK_BYTES,
    RUNTIME_LOCK_FILENAME,
    RUNTIME_SIGNATURE_FILENAME,
    RuntimeLock,
    load_runtime_lock,
    verify_runtime_bundle,
)

SIGNATURE_SCHEMA_VERSION = 1
SIGNATURE_DOMAIN = b"remote-mcp-commander/runtime-lock/v1\x00"
SIGNATURE_DOMAIN_NAME = "remote-mcp-commander/runtime-lock/v1"
MAX_SIGNATURE_BYTES = 16 * 1024
MAX_KEY_BYTES = 16 * 1024
KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class RuntimeSigningError(RuntimeError):
    pass


class RuntimeSignature(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = SIGNATURE_SCHEMA_VERSION
    algorithm: Literal["ed25519"] = "ed25519"
    domain: Literal["remote-mcp-commander/runtime-lock/v1"] = SIGNATURE_DOMAIN_NAME
    key_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature: str = Field(min_length=1, max_length=256)


def _safe_file_bytes(
    path: Path,
    *,
    limit: int,
    label: str,
    forbidden_mode_mask: int = 0,
) -> bytes:
    if path.is_symlink():
        raise RuntimeSigningError(f"{label} must be a regular non-symlink file")
    flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RuntimeSigningError(f"cannot open {label} safely") from exc
    chunks: list[bytes] = []
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeSigningError(f"{label} must be a regular non-symlink file")
        if before.st_size < 1 or before.st_size > limit:
            raise RuntimeSigningError(f"{label} size is outside allowed bounds")
        if os.name != "nt" and stat.S_IMODE(before.st_mode) & forbidden_mode_mask:
            raise RuntimeSigningError(f"{label} has unsafe file permissions")
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                raise RuntimeSigningError(f"{label} changed while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after:
            raise RuntimeSigningError(f"{label} changed while being read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _lock_bytes(runtime_bundle: Path) -> bytes:
    try:
        return _read_small_regular(
            runtime_bundle / RUNTIME_LOCK_FILENAME,
            limit=MAX_LOCK_BYTES,
            label="runtime lock",
        )
    except (OSError, PackagingError) as exc:
        raise RuntimeSigningError("cannot read runtime lock safely") from exc


def _resolve_runtime_bundle(runtime_bundle: Path) -> Path:
    bundle_input = runtime_bundle.expanduser()
    if bundle_input.is_symlink():
        raise RuntimeSigningError("runtime bundle must not be a symlink")
    try:
        resolved = bundle_input.resolve(strict=True)
    except OSError as exc:
        raise RuntimeSigningError("runtime bundle path is invalid") from exc
    if not resolved.is_dir():
        raise RuntimeSigningError("runtime bundle must be a directory")
    return resolved


def _write_readonly_json(path: Path, payload: dict[str, object]) -> None:
    if path.exists() or path.is_symlink():
        raise RuntimeSigningError("runtime signature already exists")
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if len(data) > MAX_SIGNATURE_BYTES:
        raise RuntimeSigningError("runtime signature exceeds the size limit")
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
        label="runtime private signing key",
        forbidden_mode_mask=0o077,
    )
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (TypeError, ValueError) as exc:
        raise RuntimeSigningError("runtime private key is not a valid unencrypted PEM") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise RuntimeSigningError("runtime private signing key must be Ed25519")
    return key


def _trusted_key_path(trusted_keys_dir: Path, key_id: str) -> Path:
    if not KEY_ID_RE.fullmatch(key_id):
        raise RuntimeSigningError("invalid runtime signing key ID")
    if trusted_keys_dir.is_symlink() or not trusted_keys_dir.is_dir():
        raise RuntimeSigningError("trusted runtime key directory is missing or unsafe")
    if os.name != "nt" and stat.S_IMODE(trusted_keys_dir.stat().st_mode) & 0o022:
        raise RuntimeSigningError("trusted runtime key directory must not be group/world writable")
    return trusted_keys_dir / f"{key_id}.pem"


def _load_public_key(trusted_keys_dir: Path, key_id: str) -> Ed25519PublicKey:
    data = _safe_file_bytes(
        _trusted_key_path(trusted_keys_dir, key_id),
        limit=MAX_KEY_BYTES,
        label="trusted runtime public key",
        forbidden_mode_mask=0o022,
    )
    try:
        key = serialization.load_pem_public_key(data)
    except ValueError as exc:
        raise RuntimeSigningError("trusted runtime public key is invalid") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise RuntimeSigningError("trusted runtime public key must be Ed25519")
    return key


def load_runtime_signature(runtime_bundle: Path) -> RuntimeSignature:
    data = _safe_file_bytes(
        runtime_bundle / RUNTIME_SIGNATURE_FILENAME,
        limit=MAX_SIGNATURE_BYTES,
        label="runtime signature",
        forbidden_mode_mask=0o222,
    )

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise RuntimeSigningError("runtime signature contains duplicate JSON keys")
            result[key] = value
        return result

    try:
        payload = json.loads(data, object_pairs_hook=reject_duplicates)
        result = RuntimeSignature.model_validate(payload)
        raw_signature = base64.b64decode(result.signature, validate=True)
    except RuntimeSigningError:
        raise
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeSigningError("runtime signature is invalid") from exc
    if len(raw_signature) != 64:
        raise RuntimeSigningError("Ed25519 runtime signature has an invalid length")
    return result


def sign_runtime_bundle(
    runtime_bundle: Path,
    *,
    package_bundle: Path,
    trusted_package_keys: Path,
    key_id: str,
    private_key_path: Path,
    python_executable: str = sys.executable,
) -> RuntimeSignature:
    if not KEY_ID_RE.fullmatch(key_id):
        raise RuntimeSigningError("invalid runtime signing key ID")
    resolved_bundle = _resolve_runtime_bundle(runtime_bundle)
    private_input = private_key_path.expanduser()
    if private_input.is_symlink():
        raise RuntimeSigningError("runtime private signing key must not be a symlink")
    try:
        resolved_private = private_input.resolve(strict=True)
    except OSError as exc:
        raise RuntimeSigningError("runtime private signing key path is invalid") from exc
    if resolved_private.is_relative_to(resolved_bundle):
        raise RuntimeSigningError("runtime private signing key must be outside the bundle")

    verified_lock = verify_runtime_bundle(
        runtime_bundle=resolved_bundle,
        package_bundle=package_bundle,
        trusted_package_keys=trusted_package_keys,
        python_executable=python_executable,
    )
    lock_bytes = _lock_bytes(resolved_bundle)
    if load_runtime_lock(resolved_bundle) != verified_lock:
        raise RuntimeSigningError("runtime lock changed before signing")
    key = _load_private_key(resolved_private)
    signature = RuntimeSignature(
        key_id=key_id,
        lock_sha256=hashlib.sha256(lock_bytes).hexdigest(),
        signature=base64.b64encode(key.sign(SIGNATURE_DOMAIN + lock_bytes)).decode("ascii"),
    )
    _write_readonly_json(
        resolved_bundle / RUNTIME_SIGNATURE_FILENAME,
        signature.model_dump(mode="json"),
    )
    return load_runtime_signature(resolved_bundle)


def verify_signed_runtime_bundle(
    runtime_bundle: Path,
    *,
    package_bundle: Path,
    trusted_package_keys: Path,
    trusted_runtime_keys: Path,
    python_executable: str = sys.executable,
) -> tuple[RuntimeLock, RuntimeSignature]:
    resolved_bundle = _resolve_runtime_bundle(runtime_bundle)
    trust_input = trusted_runtime_keys.expanduser()
    if trust_input.is_symlink():
        raise RuntimeSigningError("trusted runtime key directory must not be a symlink")
    try:
        resolved_trust = trust_input.resolve(strict=True)
    except OSError as exc:
        raise RuntimeSigningError("trusted runtime key directory is missing or unsafe") from exc
    if resolved_trust.is_relative_to(resolved_bundle):
        raise RuntimeSigningError("trusted runtime keys must be outside the bundle")

    signature = load_runtime_signature(resolved_bundle)
    lock_bytes = _lock_bytes(resolved_bundle)
    lock_sha256 = hashlib.sha256(lock_bytes).hexdigest()
    if lock_sha256 != signature.lock_sha256:
        raise RuntimeSigningError("runtime signature references a different lock")
    public_key = _load_public_key(resolved_trust, signature.key_id)
    try:
        raw_signature = base64.b64decode(signature.signature, validate=True)
        public_key.verify(raw_signature, SIGNATURE_DOMAIN + lock_bytes)
    except (InvalidSignature, ValueError) as exc:
        raise RuntimeSigningError("runtime lock signature verification failed") from exc

    lock = verify_runtime_bundle(
        runtime_bundle=resolved_bundle,
        package_bundle=package_bundle,
        trusted_package_keys=trusted_package_keys,
        python_executable=python_executable,
    )
    if hashlib.sha256(_lock_bytes(resolved_bundle)).hexdigest() != lock_sha256:
        raise RuntimeSigningError("runtime lock changed during signed verification")
    if load_runtime_signature(resolved_bundle) != signature:
        raise RuntimeSigningError("runtime signature changed during signed verification")
    return lock, signature

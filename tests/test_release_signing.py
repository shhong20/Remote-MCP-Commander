from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from remote_mcp_commander.release_integrity import (
    MANIFEST_FILENAME,
    SIGNATURE_FILENAME,
    seal_release,
    verify_release,
)
from remote_mcp_commander.release_signing import (
    SigningError,
    load_signature,
    sign_release,
    verify_signed_release,
)


def make_candidate(tmp_path: Path, release_id: str = "v1") -> Path:
    release = tmp_path / release_id
    doctor = release / ".venv" / "bin" / "remote-mcp-doctor"
    doctor.parent.mkdir(parents=True)
    doctor.write_text("#!/bin/sh\nexit 0\n")
    doctor.chmod(0o755)
    (release / "pyproject.toml").write_text('[project]\nname="test-release"\nversion="0.18.0"\n')
    package = release / "src" / "remote_mcp_commander"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "0.18.0"\n')
    (package / "protocol.py").write_text("PROTOCOL_MIN_SUPPORTED = 1\nPROTOCOL_MAX_SUPPORTED = 1\n")
    seal_release(release, commit_sha="d" * 40)
    return release


def make_keypair(tmp_path: Path, key_id: str = "ops-2026") -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    private = Ed25519PrivateKey.generate()
    private_path = tmp_path / f"{key_id}-private.pem"
    private_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    private_path.chmod(0o600)
    trusted = tmp_path / "trusted-release-keys"
    trusted.mkdir(mode=0o755)
    public_path = trusted / f"{key_id}.pem"
    public_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    public_path.chmod(0o644)
    return private_path, trusted


def test_sign_and_verify_ed25519_release(tmp_path: Path) -> None:
    release = make_candidate(tmp_path / "artifact")
    private_key, trusted = make_keypair(tmp_path)

    signature = sign_release(release, key_id="ops-2026", private_key_path=private_key)
    manifest, verified = verify_signed_release(release, trusted_keys_dir=trusted)

    assert signature.key_id == "ops-2026"
    assert verified == signature
    assert manifest.release_id == "v1"
    assert verify_release(release).release_id == "v1"
    if os.name != "nt":
        assert (release / SIGNATURE_FILENAME).stat().st_mode & 0o222 == 0


def test_wrong_trusted_key_rejects_signature(tmp_path: Path) -> None:
    release = make_candidate(tmp_path / "artifact")
    private_key, trusted = make_keypair(tmp_path / "signer")
    sign_release(release, key_id="ops-2026", private_key_path=private_key)

    wrong_root = tmp_path / "wrong"
    _, wrong_trusted = make_keypair(wrong_root)
    with pytest.raises(SigningError, match="verification failed"):
        verify_signed_release(release, trusted_keys_dir=wrong_trusted)


def test_manifest_rewrite_after_signing_is_rejected(tmp_path: Path) -> None:
    release = make_candidate(tmp_path / "artifact")
    private_key, trusted = make_keypair(tmp_path)
    sign_release(release, key_id="ops-2026", private_key_path=private_key)

    manifest_path = release / MANIFEST_FILENAME
    manifest_path.chmod(0o600)
    payload = json.loads(manifest_path.read_text())
    payload["created_at"] = "2030-01-01T00:00:00Z"
    manifest_path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    manifest_path.chmod(0o444)

    with pytest.raises(SigningError, match="different manifest|verification failed"):
        verify_signed_release(release, trusted_keys_dir=trusted)


def test_writable_signature_is_rejected(tmp_path: Path) -> None:
    release = make_candidate(tmp_path / "artifact")
    private_key, _ = make_keypair(tmp_path)
    sign_release(release, key_id="ops-2026", private_key_path=private_key)
    signature_path = release / SIGNATURE_FILENAME
    signature_path.chmod(0o644)

    if os.name != "nt":
        with pytest.raises(SigningError, match="unsafe file permissions"):
            load_signature(release)


def test_private_key_must_not_be_group_or_world_accessible(tmp_path: Path) -> None:
    release = make_candidate(tmp_path / "artifact")
    private_key, _ = make_keypair(tmp_path)
    private_key.chmod(0o644)

    if os.name != "nt":
        with pytest.raises(SigningError, match="unsafe file permissions"):
            sign_release(release, key_id="ops-2026", private_key_path=private_key)


def test_private_key_must_be_outside_release(tmp_path: Path) -> None:
    release = make_candidate(tmp_path / "artifact")
    private_key, _ = make_keypair(tmp_path)
    inside = release / "private.pem"
    inside.write_bytes(private_key.read_bytes())
    inside.chmod(0o600)

    with pytest.raises(SigningError, match="outside the release"):
        sign_release(release, key_id="ops-2026", private_key_path=inside)


def test_trusted_keys_must_be_outside_release(tmp_path: Path) -> None:
    release = make_candidate(tmp_path / "artifact")
    private_key, trusted = make_keypair(tmp_path)
    sign_release(release, key_id="ops-2026", private_key_path=private_key)
    inside = release / "trusted-release-keys"
    inside.mkdir()
    (inside / "ops-2026.pem").write_bytes((trusted / "ops-2026.pem").read_bytes())

    with pytest.raises(SigningError, match="outside the release"):
        verify_signed_release(release, trusted_keys_dir=inside)


def test_group_writable_trust_directory_is_rejected(tmp_path: Path) -> None:
    release = make_candidate(tmp_path / "artifact")
    private_key, trusted = make_keypair(tmp_path)
    sign_release(release, key_id="ops-2026", private_key_path=private_key)
    trusted.chmod(0o775)

    if os.name != "nt":
        with pytest.raises(SigningError, match="group/world writable"):
            verify_signed_release(release, trusted_keys_dir=trusted)


def test_private_key_symlink_is_rejected(tmp_path: Path) -> None:
    release = make_candidate(tmp_path / "artifact")
    private_key, _ = make_keypair(tmp_path)
    link = tmp_path / "private-link.pem"
    link.symlink_to(private_key)

    with pytest.raises(SigningError, match="must not be a symlink"):
        sign_release(release, key_id="ops-2026", private_key_path=link)


def test_trust_directory_symlink_is_rejected(tmp_path: Path) -> None:
    release = make_candidate(tmp_path / "artifact")
    private_key, trusted = make_keypair(tmp_path / "keys")
    sign_release(release, key_id="ops-2026", private_key_path=private_key)
    link = tmp_path / "trusted-link"
    link.symlink_to(trusted, target_is_directory=True)

    with pytest.raises(SigningError, match="must not be a symlink"):
        verify_signed_release(release, trusted_keys_dir=link)


def test_removing_trusted_key_revokes_release(tmp_path: Path) -> None:
    release = make_candidate(tmp_path / "artifact")
    private_key, trusted = make_keypair(tmp_path)
    sign_release(release, key_id="ops-2026", private_key_path=private_key)
    (trusted / "ops-2026.pem").unlink()

    with pytest.raises(SigningError, match="trusted release public key"):
        verify_signed_release(release, trusted_keys_dir=trusted)


def test_writable_trusted_public_key_is_rejected(tmp_path: Path) -> None:
    release = make_candidate(tmp_path / "artifact")
    private_key, trusted = make_keypair(tmp_path)
    sign_release(release, key_id="ops-2026", private_key_path=private_key)
    public_key = trusted / "ops-2026.pem"
    public_key.chmod(0o666)

    if os.name != "nt":
        with pytest.raises(SigningError, match="unsafe file permissions"):
            verify_signed_release(release, trusted_keys_dir=trusted)


def test_malformed_base64_signature_fails_closed(tmp_path: Path) -> None:
    release = make_candidate(tmp_path / "artifact")
    private_key, trusted = make_keypair(tmp_path)
    sign_release(release, key_id="ops-2026", private_key_path=private_key)
    path = release / SIGNATURE_FILENAME
    path.chmod(0o600)
    payload = json.loads(path.read_text())
    payload["signature"] = "not-valid-base64!!!"
    path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    path.chmod(0o444)

    with pytest.raises(SigningError, match="invalid"):
        verify_signed_release(release, trusted_keys_dir=trusted)

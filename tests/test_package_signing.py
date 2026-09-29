from __future__ import annotations

import base64
import os
import shutil
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from remote_mcp_commander.package import (
    PACKAGE_MANIFEST,
    PackagingError,
    build_bundle,
    verify_bundle,
)
from remote_mcp_commander.package_signing import (
    PACKAGE_SIGNATURE,
    SIGNATURE_DOMAIN,
    PackageSigningError,
    load_package_signature,
    sign_package_bundle,
    verify_signed_package_bundle,
)


@pytest.fixture(scope="module")
def unsigned_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("package-signing")
    repo = Path(__file__).resolve().parents[1]
    bundle = root / "bundle"
    build_bundle(repo=repo, commit="HEAD", output=bundle)
    return bundle


def copy_bundle(source: Path, tmp_path: Path) -> Path:
    target = tmp_path / "bundle"
    shutil.copytree(source, target)
    return target


def make_keypair(root: Path, *, key_id: str = "package-2026") -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    private = Ed25519PrivateKey.generate()
    private_path = root / f"{key_id}-private.pem"
    private_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    if os.name != "nt":
        private_path.chmod(0o600)
    trusted = root / "trusted-package-keys"
    trusted.mkdir()
    if os.name != "nt":
        trusted.chmod(0o755)
    public_path = trusted / f"{key_id}.pem"
    public_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    if os.name != "nt":
        public_path.chmod(0o644)
    return private_path, trusted


def test_valid_package_signature_and_plain_verify(unsigned_bundle: Path, tmp_path: Path) -> None:
    bundle = copy_bundle(unsigned_bundle, tmp_path)
    private_key, trusted = make_keypair(tmp_path / "keys")

    signature = sign_package_bundle(bundle, key_id="package-2026", private_key_path=private_key)
    plain = verify_bundle(bundle)
    trusted_manifest, trusted_signature = verify_signed_package_bundle(
        bundle, trusted_keys_dir=trusted
    )

    assert plain == trusted_manifest
    assert trusted_signature == signature


def test_package_signature_is_domain_separated(unsigned_bundle: Path, tmp_path: Path) -> None:
    bundle = copy_bundle(unsigned_bundle, tmp_path)
    private_key, trusted = make_keypair(tmp_path / "keys")
    signature = sign_package_bundle(bundle, key_id="package-2026", private_key_path=private_key)
    public = serialization.load_pem_public_key((trusted / "package-2026.pem").read_bytes())
    raw_signature = base64.b64decode(signature.signature)
    manifest_bytes = (bundle / PACKAGE_MANIFEST).read_bytes()

    with pytest.raises(InvalidSignature):
        public.verify(raw_signature, manifest_bytes)
    public.verify(raw_signature, SIGNATURE_DOMAIN + manifest_bytes)


def test_wrong_trusted_key_is_rejected(unsigned_bundle: Path, tmp_path: Path) -> None:
    bundle = copy_bundle(unsigned_bundle, tmp_path)
    private_key, _ = make_keypair(tmp_path / "signer")
    sign_package_bundle(bundle, key_id="package-2026", private_key_path=private_key)
    _, wrong_trust = make_keypair(tmp_path / "wrong")

    with pytest.raises(PackageSigningError, match="verification failed"):
        verify_signed_package_bundle(bundle, trusted_keys_dir=wrong_trust)


def test_key_revocation_is_immediate(unsigned_bundle: Path, tmp_path: Path) -> None:
    bundle = copy_bundle(unsigned_bundle, tmp_path)
    private_key, trusted = make_keypair(tmp_path / "keys")
    sign_package_bundle(bundle, key_id="package-2026", private_key_path=private_key)
    (trusted / "package-2026.pem").unlink()

    with pytest.raises(PackageSigningError, match="trusted package public key"):
        verify_signed_package_bundle(bundle, trusted_keys_dir=trusted)


def test_artifact_tamper_is_rejected_after_signature(unsigned_bundle: Path, tmp_path: Path) -> None:
    bundle = copy_bundle(unsigned_bundle, tmp_path)
    private_key, trusted = make_keypair(tmp_path / "keys")
    sign_package_bundle(bundle, key_id="package-2026", private_key_path=private_key)
    wheel = next(bundle.glob("*.whl"))
    with wheel.open("ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(PackagingError, match="does not match manifest"):
        verify_signed_package_bundle(bundle, trusted_keys_dir=trusted)


def test_manifest_tamper_is_rejected_before_artifact_use(
    unsigned_bundle: Path, tmp_path: Path
) -> None:
    bundle = copy_bundle(unsigned_bundle, tmp_path)
    private_key, trusted = make_keypair(tmp_path / "keys")
    sign_package_bundle(bundle, key_id="package-2026", private_key_path=private_key)
    manifest = bundle / PACKAGE_MANIFEST
    manifest.write_bytes(manifest.read_bytes() + b" ")

    with pytest.raises(PackageSigningError, match="different manifest"):
        verify_signed_package_bundle(bundle, trusted_keys_dir=trusted)


def test_private_key_must_be_outside_bundle(unsigned_bundle: Path, tmp_path: Path) -> None:
    bundle = copy_bundle(unsigned_bundle, tmp_path)
    private_key, _ = make_keypair(tmp_path / "keys")
    inside = bundle / "private.pem"
    inside.write_bytes(private_key.read_bytes())
    if os.name != "nt":
        inside.chmod(0o600)

    with pytest.raises(PackageSigningError, match="outside the bundle"):
        sign_package_bundle(bundle, key_id="package-2026", private_key_path=inside)


def test_trusted_keys_must_be_outside_bundle(unsigned_bundle: Path, tmp_path: Path) -> None:
    bundle = copy_bundle(unsigned_bundle, tmp_path)
    private_key, trusted = make_keypair(tmp_path / "keys")
    sign_package_bundle(bundle, key_id="package-2026", private_key_path=private_key)
    inside = bundle / "trusted-package-keys"
    inside.mkdir()
    shutil.copyfile(trusted / "package-2026.pem", inside / "package-2026.pem")

    with pytest.raises(PackageSigningError, match="outside the bundle"):
        verify_signed_package_bundle(bundle, trusted_keys_dir=inside)


def test_writable_signature_is_rejected(unsigned_bundle: Path, tmp_path: Path) -> None:
    bundle = copy_bundle(unsigned_bundle, tmp_path)
    private_key, _ = make_keypair(tmp_path / "keys")
    sign_package_bundle(bundle, key_id="package-2026", private_key_path=private_key)
    signature_path = bundle / PACKAGE_SIGNATURE
    if os.name != "nt":
        signature_path.chmod(0o644)
        with pytest.raises(PackageSigningError, match="unsafe file permissions"):
            load_package_signature(bundle)


def test_duplicate_signature_json_keys_are_rejected(unsigned_bundle: Path, tmp_path: Path) -> None:
    bundle = copy_bundle(unsigned_bundle, tmp_path)
    private_key, _ = make_keypair(tmp_path / "keys")
    sign_package_bundle(bundle, key_id="package-2026", private_key_path=private_key)
    signature_path = bundle / PACKAGE_SIGNATURE
    if os.name != "nt":
        signature_path.chmod(0o600)
    signature_path.write_text('{"schema_version":1,"schema_version":1}')
    if os.name != "nt":
        signature_path.chmod(0o444)

    with pytest.raises(PackageSigningError, match="duplicate JSON keys"):
        load_package_signature(bundle)


def test_private_key_permissions_are_owner_only(unsigned_bundle: Path, tmp_path: Path) -> None:
    bundle = copy_bundle(unsigned_bundle, tmp_path)
    private_key, _ = make_keypair(tmp_path / "keys")
    if os.name != "nt":
        private_key.chmod(0o644)
        with pytest.raises(PackageSigningError, match="unsafe file permissions"):
            sign_package_bundle(bundle, key_id="package-2026", private_key_path=private_key)


def test_trust_directory_must_not_be_group_writable(unsigned_bundle: Path, tmp_path: Path) -> None:
    bundle = copy_bundle(unsigned_bundle, tmp_path)
    private_key, trusted = make_keypair(tmp_path / "keys")
    sign_package_bundle(bundle, key_id="package-2026", private_key_path=private_key)
    if os.name != "nt":
        trusted.chmod(0o775)
        with pytest.raises(PackageSigningError, match="group/world writable"):
            verify_signed_package_bundle(bundle, trusted_keys_dir=trusted)


def test_package_cli_sign_and_verify_trusted(unsigned_bundle: Path, tmp_path: Path, capsys) -> None:
    from remote_mcp_commander.package import main

    bundle = copy_bundle(unsigned_bundle, tmp_path)
    private_key, trusted = make_keypair(tmp_path / "keys")
    assert (
        main(
            [
                "sign",
                "--bundle",
                str(bundle),
                "--key-id",
                "package-2026",
                "--private-key",
                str(private_key),
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "verify-trusted",
                "--bundle",
                str(bundle),
                "--trusted-keys-dir",
                str(trusted),
                "--json",
            ]
        )
        == 0
    )
    assert "package-2026" in capsys.readouterr().out

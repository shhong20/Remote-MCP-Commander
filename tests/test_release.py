from __future__ import annotations

import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from remote_mcp_commander.release import (
    ReleaseError,
    _validate_root,
    activate,
    main,
    rollback,
    status,
)
from remote_mcp_commander.release_integrity import IntegrityError, seal_release
from remote_mcp_commander.release_signing import SigningError, sign_release


def make_release(root: Path, release_id: str) -> Path:
    release = root / "releases" / release_id
    doctor = release / ".venv" / "bin" / "remote-mcp-doctor"
    doctor.parent.mkdir(parents=True)
    doctor.write_text("#!/bin/sh\nexit 0\n")
    doctor.chmod(0o755)
    (release / "pyproject.toml").write_text('[project]\nname="test-release"\nversion="0.16.0"\n')
    package = release / "src" / "remote_mcp_commander"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "0.16.0"\n')
    (package / "protocol.py").write_text("PROTOCOL_MIN_SUPPORTED = 1\nPROTOCOL_MAX_SUPPORTED = 1\n")
    seal_release(release, commit_sha="a" * 40)
    root = release.parent.parent
    sign_release(
        release,
        key_id="test-key",
        private_key_path=root / "test-signing-private.pem",
    )
    return release


def make_root(tmp_path: Path) -> Path:
    root = tmp_path / "commander"
    (root / "releases").mkdir(parents=True)
    private = Ed25519PrivateKey.generate()
    private_path = root / "test-signing-private.pem"
    private_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    private_path.chmod(0o600)
    trusted = root / "trusted-release-keys"
    trusted.mkdir(mode=0o755)
    public_path = trusted / "test-key.pem"
    public_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    public_path.chmod(0o644)
    return root


def test_activate_tracks_previous_and_rollback_swaps(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    make_release(root, "v1")
    make_release(root, "v2")

    first = activate(_validate_root(str(root)), "v1")
    assert first.current == "v1"
    assert first.previous is None

    second = activate(root, "v2")
    assert second.current == "v2"
    assert second.previous == "v1"
    assert os.readlink(root / "current") == "releases/v2"

    rolled_back = rollback(root)
    assert rolled_back.current == "v1"
    assert rolled_back.previous == "v2"
    assert os.readlink(root / "current") == "releases/v1"


def test_activate_is_idempotent_for_current_release(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    make_release(root, "v1")
    activate(root, "v1")

    again = activate(root, "v1")
    assert again.current == "v1"
    assert again.previous is None


def test_release_id_traversal_is_rejected(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    with pytest.raises(ReleaseError, match="invalid release ID"):
        activate(root, "../outside")


def test_symlink_release_directory_is_rejected(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "releases" / "evil").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ReleaseError, match="must not be a symlink"):
        activate(root, "evil")


def test_release_requires_doctor_entrypoint(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    (root / "releases" / "broken").mkdir()

    with pytest.raises(ReleaseError, match="remote-mcp-doctor"):
        activate(root, "broken")


def test_status_rejects_current_link_outside_release_directory(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    make_release(root, "v1")
    (root / "current").symlink_to("../outside")

    with pytest.raises(ReleaseError, match="outside the releases directory"):
        status(root)


def test_status_lists_only_activation_ready_releases(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    make_release(root, "ready")
    (root / "releases" / "broken").mkdir()

    result = status(root)
    assert result.available == ["ready"]


def test_root_symlink_is_rejected(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    link = tmp_path / "linked-root"
    link.symlink_to(root, target_is_directory=True)

    with pytest.raises(ReleaseError, match="root must not be a symlink"):
        _validate_root(str(link))


def test_status_distinguishes_sealed_candidates(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    make_release(root, "sealed")
    unsealed = root / "releases" / "unsealed"
    doctor = unsealed / ".venv" / "bin" / "remote-mcp-doctor"
    doctor.parent.mkdir(parents=True)
    doctor.write_text("#!/bin/sh\nexit 0\n")
    doctor.chmod(0o755)

    result = status(root)

    assert result.available == ["sealed", "unsealed"]
    assert result.sealed == ["sealed"]
    assert result.signed == ["sealed"]


def test_cli_seal_verify_and_activate(tmp_path: Path, capsys) -> None:
    root = make_root(tmp_path)
    release = root / "releases" / "v1"
    doctor = release / ".venv" / "bin" / "remote-mcp-doctor"
    doctor.parent.mkdir(parents=True)
    doctor.write_text("#!/bin/sh\nexit 0\n")
    doctor.chmod(0o755)
    (release / "pyproject.toml").write_text('[project]\nname="test-release"\nversion="0.17.0"\n')
    package = release / "src" / "remote_mcp_commander"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "0.17.0"\n')
    (package / "protocol.py").write_text("PROTOCOL_MIN_SUPPORTED = 1\nPROTOCOL_MAX_SUPPORTED = 1\n")

    assert main(["--root", str(root), "seal", "v1", "--commit-sha", "c" * 40]) == 0
    assert "tree_sha256:" in capsys.readouterr().out
    assert main(["--root", str(root), "verify-integrity", "v1"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "--root",
                str(root),
                "sign",
                "v1",
                "--key-id",
                "test-key",
                "--private-key",
                str(root / "test-signing-private.pem"),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["--root", str(root), "verify", "v1"]) == 0
    capsys.readouterr()
    assert main(["--root", str(root), "activate", "v1"]) == 0
    assert status(root).current == "v1"


def test_rollback_rejects_tampered_previous_release(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    first = make_release(root, "v1")
    make_release(root, "v2")
    activate(root, "v1")
    activate(root, "v2")
    (first / "pyproject.toml").chmod(0o600)
    (first / "pyproject.toml").write_text('[project]\nname="changed"\nversion="0.16.0"\n')

    with pytest.raises(IntegrityError, match="manifest|tree|metadata"):
        rollback(root)


def test_activate_rejects_unsealed_release(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    release = root / "releases" / "legacy"
    doctor = release / ".venv" / "bin" / "remote-mcp-doctor"
    doctor.parent.mkdir(parents=True)
    doctor.write_text("#!/bin/sh\nexit 0\n")
    doctor.chmod(0o755)

    with pytest.raises(SigningError, match="signature"):
        activate(root, "legacy")


def test_activate_rejects_sealed_but_unsigned_release(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    release = root / "releases" / "unsigned"
    doctor = release / ".venv" / "bin" / "remote-mcp-doctor"
    doctor.parent.mkdir(parents=True)
    doctor.write_text("#!/bin/sh\nexit 0\n")
    doctor.chmod(0o755)
    (release / "pyproject.toml").write_text('[project]\nname="test-release"\nversion="0.18.0"\n')
    package = release / "src" / "remote_mcp_commander"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "0.18.0"\n')
    (package / "protocol.py").write_text("PROTOCOL_MIN_SUPPORTED = 1\nPROTOCOL_MAX_SUPPORTED = 1\n")
    seal_release(release, commit_sha="e" * 40)

    with pytest.raises(SigningError, match="signature"):
        activate(root, "unsigned")

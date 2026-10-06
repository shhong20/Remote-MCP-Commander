from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import remote_mcp_commander.release as release_module
from remote_mcp_commander.release import (
    HealthRollbackError,
    ReleaseError,
    _validate_root,
    activate,
    activate_checked,
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
    release_module.NATIVE_RELEASE_ROOT = root.resolve()
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


def test_checked_activation_restarts_and_confirms_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_root(tmp_path)
    make_release(root, "v1")
    make_release(root, "v2")
    activate(root, "v1")
    restarts: list[str] = []
    monkeypatch.setattr(
        release_module,
        "_restart_native_stack",
        lambda: restarts.append("restart"),
    )
    monkeypatch.setattr(
        release_module,
        "_wait_for_native_stack",
        lambda **_kwargs: 2,
    )

    result = activate_checked(root, "v2", timeout_seconds=5, interval_seconds=0.1)

    assert result.current == "v2"
    assert result.previous == "v1"
    assert restarts == ["restart"]


def test_checked_activation_automatically_rolls_back_failed_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_root(tmp_path)
    make_release(root, "v1")
    make_release(root, "v2")
    activate(root, "v1")
    restarts: list[str] = []
    health_results = iter([None, 1])
    monkeypatch.setattr(
        release_module,
        "_restart_native_stack",
        lambda: restarts.append("restart"),
    )
    monkeypatch.setattr(
        release_module,
        "_wait_for_native_stack",
        lambda **_kwargs: next(health_results),
    )

    with pytest.raises(HealthRollbackError) as captured:
        activate_checked(root, "v2", timeout_seconds=5, interval_seconds=0.1)

    assert captured.value.rolled_back is True
    assert captured.value.recovery_healthy is True
    assert status(root).current == "v1"
    assert status(root).previous == "v2"
    assert restarts == ["restart", "restart"]


def test_checked_activation_reports_failed_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_root(tmp_path)
    make_release(root, "v1")
    make_release(root, "v2")
    activate(root, "v1")
    health_results = iter([None, None])
    monkeypatch.setattr(release_module, "_restart_native_stack", lambda: None)
    monkeypatch.setattr(
        release_module,
        "_wait_for_native_stack",
        lambda **_kwargs: next(health_results),
    )

    with pytest.raises(HealthRollbackError) as captured:
        activate_checked(root, "v2", timeout_seconds=5, interval_seconds=0.1)

    assert captured.value.rolled_back is True
    assert captured.value.recovery_healthy is False
    assert status(root).current == "v1"
    assert status(root).previous == "v2"


def test_checked_activation_rolls_back_when_candidate_restart_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_root(tmp_path)
    make_release(root, "v1")
    make_release(root, "v2")
    activate(root, "v1")
    restart_count = 0

    def restart() -> None:
        nonlocal restart_count
        restart_count += 1
        if restart_count == 1:
            raise ReleaseError("candidate restart failed")

    monkeypatch.setattr(release_module, "_restart_native_stack", restart)
    monkeypatch.setattr(
        release_module,
        "_wait_for_native_stack",
        lambda **_kwargs: 1,
    )

    with pytest.raises(HealthRollbackError) as captured:
        activate_checked(root, "v2", timeout_seconds=5, interval_seconds=0.1)

    assert captured.value.rolled_back is True
    assert captured.value.recovery_healthy is True
    assert status(root).current == "v1"
    assert restart_count == 2


def test_checked_activation_requires_rollback_target_and_new_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_root(tmp_path)
    make_release(root, "v1")
    make_release(root, "v2")
    monkeypatch.setattr(release_module, "_restart_native_stack", lambda: None)
    monkeypatch.setattr(
        release_module,
        "_wait_for_native_stack",
        lambda **_kwargs: 1,
    )

    with pytest.raises(ReleaseError, match="existing current"):
        activate_checked(root, "v2")
    activate(root, "v1")
    with pytest.raises(ReleaseError, match="different candidate"):
        activate_checked(root, "v1")
    with pytest.raises(ReleaseError, match="health timeout"):
        activate_checked(root, "v2", timeout_seconds=0.5)


def test_checked_activation_verifies_current_before_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_root(tmp_path)
    current = make_release(root, "v1")
    make_release(root, "v2")
    activate(root, "v1")
    (current / "pyproject.toml").chmod(0o600)
    (current / "pyproject.toml").write_text('[project]\nname="changed"\nversion="0.16.0"\n')
    monkeypatch.setattr(release_module, "_restart_native_stack", lambda: None)

    with pytest.raises(IntegrityError):
        activate_checked(root, "v2")

    assert os.readlink(root / "current") == "releases/v1"


def test_checked_activation_cli_reports_rollback_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    root = make_root(tmp_path)
    make_release(root, "v1")
    make_release(root, "v2")
    activate(root, "v1")
    health_results = iter([None, 1])
    monkeypatch.setattr(release_module, "_restart_native_stack", lambda: None)
    monkeypatch.setattr(
        release_module,
        "_wait_for_native_stack",
        lambda **_kwargs: next(health_results),
    )

    assert (
        main(
            [
                "--root",
                str(root),
                "--json",
                "activate-checked",
                "v2",
                "--timeout-seconds",
                "5",
                "--interval-seconds",
                "0.1",
            ]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["rolled_back"] is True
    assert payload["recovery_healthy"] is True
    assert status(root).current == "v1"


def test_native_stack_restart_uses_fixed_systemd_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    systemctl = tmp_path / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n")
    systemctl.chmod(0o755)
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(release_module, "SYSTEMCTL_PATH", systemctl)
    monkeypatch.setattr(release_module.subprocess, "run", fake_run)

    release_module._restart_native_stack()

    assert captured["command"] == [
        str(systemctl),
        "restart",
        "remote-mcp-gateway.service",
        "remote-mcp-server.service",
        "--no-pager",
    ]
    assert captured["kwargs"]["env"] == {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
    }


def test_checked_activation_rejects_non_native_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_root(tmp_path)
    make_release(root, "v1")
    make_release(root, "v2")
    activate(root, "v1")
    monkeypatch.setattr(
        release_module,
        "NATIVE_RELEASE_ROOT",
        (tmp_path / "different-native-root").resolve(),
    )

    with pytest.raises(ReleaseError, match="native deployment root"):
        activate_checked(root, "v2")

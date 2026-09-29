from __future__ import annotations

import os
from pathlib import Path

import pytest

from remote_mcp_commander.release import (
    ReleaseError,
    _validate_root,
    activate,
    rollback,
    status,
)


def make_release(root: Path, release_id: str) -> Path:
    release = root / "releases" / release_id
    doctor = release / ".venv" / "bin" / "remote-mcp-doctor"
    doctor.parent.mkdir(parents=True)
    doctor.write_text("#!/bin/sh\nexit 0\n")
    doctor.chmod(0o755)
    return release


def make_root(tmp_path: Path) -> Path:
    root = tmp_path / "commander"
    (root / "releases").mkdir(parents=True)
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

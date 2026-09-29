from __future__ import annotations

import os
from pathlib import Path

import pytest

from remote_mcp_commander.release_integrity import (
    MANIFEST_FILENAME,
    IntegrityError,
    load_manifest,
    seal_release,
    verify_release,
)


def make_candidate(tmp_path: Path, release_id: str = "v1") -> Path:
    release = tmp_path / release_id
    doctor = release / ".venv" / "bin" / "remote-mcp-doctor"
    doctor.parent.mkdir(parents=True)
    doctor.write_text("#!/bin/sh\nexit 0\n")
    doctor.chmod(0o755)
    (release / "pyproject.toml").write_text('[project]\nname="test-release"\nversion="0.17.0"\n')
    package = release / "src" / "remote_mcp_commander"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "0.17.0"\n')
    (package / "protocol.py").write_text("PROTOCOL_MIN_SUPPORTED = 1\nPROTOCOL_MAX_SUPPORTED = 1\n")
    (release / "README.md").write_text("release payload\n")
    return release


def seal(release: Path):
    return seal_release(release, commit_sha="b" * 40)


def test_seal_records_metadata_and_exact_tree(tmp_path: Path) -> None:
    release = make_candidate(tmp_path)
    manifest = seal(release)

    assert manifest.release_id == "v1"
    assert manifest.package_version == "0.17.0"
    assert manifest.commit_sha == "b" * 40
    assert manifest.protocol_min == 1
    assert manifest.protocol_max == 1
    assert manifest.entries == sorted(manifest.entries, key=lambda entry: entry.path)
    assert verify_release(release).tree_sha256 == manifest.tree_sha256
    if os.name != "nt":
        assert (release / MANIFEST_FILENAME).stat().st_mode & 0o222 == 0


def test_verify_rejects_modified_file_content(tmp_path: Path) -> None:
    release = make_candidate(tmp_path)
    seal(release)
    (release / "README.md").write_text("tampered\n")

    with pytest.raises(IntegrityError, match="tree does not match"):
        verify_release(release)


def test_verify_rejects_added_or_removed_entries(tmp_path: Path) -> None:
    release = make_candidate(tmp_path)
    seal(release)
    (release / "unexpected.txt").write_text("extra")
    with pytest.raises(IntegrityError, match="tree does not match"):
        verify_release(release)

    (release / "unexpected.txt").unlink()
    (release / "README.md").unlink()
    with pytest.raises(IntegrityError, match="tree does not match"):
        verify_release(release)


def test_verify_rejects_mode_change(tmp_path: Path) -> None:
    release = make_candidate(tmp_path)
    seal(release)
    payload = release / "README.md"
    payload.chmod(0o755)

    with pytest.raises(IntegrityError, match="tree does not match"):
        verify_release(release)


def test_verify_rejects_symlink_target_change(tmp_path: Path) -> None:
    release = make_candidate(tmp_path)
    (release / "target-a.txt").write_text("a")
    (release / "target-b.txt").write_text("b")
    link = release / "runtime-link"
    link.symlink_to("target-a.txt")
    seal(release)
    link.unlink()
    link.symlink_to("target-b.txt")

    with pytest.raises(IntegrityError, match="tree does not match"):
        verify_release(release)


def test_manifest_must_remain_read_only(tmp_path: Path) -> None:
    release = make_candidate(tmp_path)
    seal(release)
    manifest_path = release / MANIFEST_FILENAME
    manifest_path.chmod(0o644)

    if os.name != "nt":
        with pytest.raises(IntegrityError, match="read-only"):
            load_manifest(release)


def test_seal_rejects_git_metadata(tmp_path: Path) -> None:
    release = make_candidate(tmp_path)
    (release / ".git").mkdir()
    (release / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    with pytest.raises(IntegrityError, match=".git metadata"):
        seal(release)


def test_verify_rejects_inconsistent_package_metadata(tmp_path: Path) -> None:
    release = make_candidate(tmp_path)
    seal(release)
    manifest_path = release / MANIFEST_FILENAME
    manifest_path.chmod(0o600)
    manifest_path.unlink()
    (release / "src" / "remote_mcp_commander" / "__init__.py").write_text('__version__ = "9.9.9"\n')

    with pytest.raises(IntegrityError, match="inconsistent"):
        seal(release)


def test_seal_rejects_existing_manifest(tmp_path: Path) -> None:
    release = make_candidate(tmp_path)
    seal(release)

    with pytest.raises(IntegrityError, match="already exists"):
        seal(release)


def test_seal_rejects_external_application_symlink(tmp_path: Path) -> None:
    release = make_candidate(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("print('outside')\n")
    (release / "src" / "remote_mcp_commander" / "escape.py").symlink_to(outside)

    with pytest.raises(IntegrityError, match="external symlink"):
        seal(release)


def test_seal_allows_venv_python_interpreter_symlink(tmp_path: Path) -> None:
    release = make_candidate(tmp_path)
    system_python = Path("/usr/bin/python3")
    if not system_python.exists():
        pytest.skip("system python symlink target is unavailable")
    python_link = release / ".venv" / "bin" / "python3"
    python_link.symlink_to(system_python)

    manifest = seal(release)

    entry = next(entry for entry in manifest.entries if entry.path == ".venv/bin/python3")
    assert entry.kind == "symlink"
    assert entry.target == str(system_python)


def test_seal_rejects_hard_linked_regular_file(tmp_path: Path) -> None:
    release = make_candidate(tmp_path)
    os.link(release / "README.md", release / "README-copy.md")

    with pytest.raises(IntegrityError, match="hard-linked"):
        seal(release)


def test_manifest_parser_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    release = make_candidate(tmp_path)
    seal(release)
    path = release / MANIFEST_FILENAME
    path.chmod(0o600)
    path.write_text('{"schema_version":1,"schema_version":1}\n')
    path.chmod(0o444)

    with pytest.raises(IntegrityError, match="duplicate JSON keys"):
        load_manifest(release)

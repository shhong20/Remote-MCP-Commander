from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from remote_mcp_commander.package import (
    BUILD_TOOLCHAIN,
    PACKAGE_MANIFEST,
    PackagingError,
    _snapshot,
    build_bundle,
    verify_bundle,
)


def test_packaging_extra_matches_build_toolchain() -> None:
    root = Path(__file__).resolve().parents[1]
    payload = tomllib.loads((root / "pyproject.toml").read_text())
    requirements = payload["project"]["optional-dependencies"]["packaging"]
    pins = dict(item.split("==", 1) for item in requirements if "==" in item)

    for package, version in BUILD_TOOLCHAIN.items():
        assert pins[package] == version

def _write_minimal_project(root: Path, *, backend: str = "setuptools.build_meta") -> None:
    package = root / "src" / "remote_mcp_commander"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "1.2.3"\n')
    (package / "protocol.py").write_text("PROTOCOL_MIN_SUPPORTED = 1\nPROTOCOL_MAX_SUPPORTED = 1\n")
    (root / "README.md").write_text("# fixture\n")
    (root / "pyproject.toml").write_text(
        "\n".join(
            [
                "[build-system]",
                'requires = ["setuptools==84.0.0"]',
                f'build-backend = "{backend}"',
                "",
                "[project]",
                'name = "remote-mcp-commander"',
                'version = "1.2.3"',
                'description = "fixture"',
                'requires-python = ">=3.11"',
                "",
                "[tool.setuptools.packages.find]",
                'where = ["src"]',
                "",
            ]
        )
    )


def _commit_repo(root: Path) -> str:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Fixture"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "fixture@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = "1700000000 +0000"
    env["GIT_COMMITTER_DATE"] = "1700000000 +0000"
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True, env=env)
    return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()


def make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_minimal_project(repo)
    return repo, _commit_repo(repo)


def test_build_bundle_is_reproducible_and_ignores_dirty_worktree(tmp_path: Path) -> None:
    repo, commit = make_repo(tmp_path)
    (repo / "src" / "remote_mcp_commander" / "protocol.py").write_text(
        "PROTOCOL_MIN_SUPPORTED = 99\nPROTOCOL_MAX_SUPPORTED = 99\n"
    )
    first = tmp_path / "bundle-a"
    second = tmp_path / "bundle-b"

    manifest_a = build_bundle(repo=repo, commit=commit, output=first, builder_python=sys.executable)
    manifest_b = build_bundle(
        repo=repo, commit=commit, output=second, builder_python=sys.executable
    )

    assert manifest_a == manifest_b
    assert manifest_a.package_version == "1.2.3"
    assert manifest_a.protocol_min == 1
    assert manifest_a.reproducibility_verified is True
    for name in {PACKAGE_MANIFEST, *(item.filename for item in manifest_a.artifacts)}:
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_verify_bundle_rejects_artifact_tampering(tmp_path: Path) -> None:
    repo, commit = make_repo(tmp_path)
    bundle = tmp_path / "bundle"
    manifest = build_bundle(repo=repo, commit=commit, output=bundle, builder_python=sys.executable)
    wheel = next(item for item in manifest.artifacts if item.kind == "wheel")
    with (bundle / wheel.filename).open("ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(PackagingError, match="does not match manifest"):
        verify_bundle(bundle)


def test_verify_bundle_rejects_unmanifested_file(tmp_path: Path) -> None:
    repo, commit = make_repo(tmp_path)
    bundle = tmp_path / "bundle"
    build_bundle(repo=repo, commit=commit, output=bundle, builder_python=sys.executable)
    (bundle / "extra.txt").write_text("unexpected")

    with pytest.raises(PackagingError, match="file set"):
        verify_bundle(bundle)


def test_snapshot_rejects_unapproved_build_backend(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_minimal_project(repo, backend="example.backend")
    commit = _commit_repo(repo)

    with pytest.raises(PackagingError, match="build backend"):
        _snapshot(repo.resolve(), commit)


def test_snapshot_rejects_external_source_symlink(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_minimal_project(repo)
    (repo / "escape").symlink_to("../outside")
    commit = _commit_repo(repo)

    with pytest.raises(PackagingError, match="symlink must stay inside"):
        _snapshot(repo.resolve(), commit)


def test_package_manifest_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / PACKAGE_MANIFEST).write_text('{"schema_version":1,"schema_version":1}')

    with pytest.raises(PackagingError, match="duplicate package manifest key"):
        verify_bundle(bundle)


def test_snapshot_rejects_dynamic_package_metadata(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_minimal_project(repo)
    pyproject = repo / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text() + '\n[tool.setuptools.dynamic]\nversion = {attr = "pkg.version"}\n'
    )
    commit = _commit_repo(repo)

    with pytest.raises(PackagingError, match="dynamic package metadata"):
        _snapshot(repo.resolve(), commit)


def test_build_bundle_rejects_missing_output_parent(tmp_path: Path) -> None:
    repo, commit = make_repo(tmp_path)
    output = tmp_path / "missing" / "bundle"

    with pytest.raises(PackagingError, match="parent directory does not exist"):
        build_bundle(repo=repo, commit=commit, output=output, builder_python=sys.executable)


def test_snapshot_rejects_project_rename(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_minimal_project(repo)
    pyproject = repo / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().replace('name = "remote-mcp-commander"', 'name = "other"')
    )
    commit = _commit_repo(repo)

    with pytest.raises(PackagingError, match="project name"):
        _snapshot(repo.resolve(), commit)

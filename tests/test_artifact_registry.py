from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from remote_mcp_commander.artifact_registry import (
    OBJECTS_DIRNAME,
    PACKAGE_DIRNAME,
    PUBLICATION_FILENAME,
    RUNTIME_DIRNAME,
    ArtifactRegistryError,
    init_registry,
    main,
    publish_artifact,
    verify_registry_artifact,
)
from remote_mcp_commander.package import PackagingError, build_bundle
from remote_mcp_commander.package_signing import sign_package_bundle
from remote_mcp_commander.runtime_lock import (
    WHEELHOUSE_DIRNAME,
    create_runtime_bundle,
)
from remote_mcp_commander.runtime_signing import (
    RuntimeSigningError,
    sign_runtime_bundle,
)


def _write_project(root: Path) -> None:
    package = root / "src" / "remote_mcp_commander"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "2.3.4"\n')
    (package / "protocol.py").write_text("PROTOCOL_MIN_SUPPORTED = 1\nPROTOCOL_MAX_SUPPORTED = 1\n")
    (root / "README.md").write_text("# artifact registry fixture\n")
    (root / "pyproject.toml").write_text(
        "\n".join(
            [
                "[build-system]",
                'requires = ["setuptools==84.0.0"]',
                'build-backend = "setuptools.build_meta"',
                "",
                "[project]",
                'name = "remote-mcp-commander"',
                'version = "2.3.4"',
                'description = "artifact registry fixture"',
                'requires-python = ">=3.11"',
                "dependencies = []",
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
        ["git", "-C", str(root), "config", "user.email", "fixture@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = "1700000000 +0000"
    env["GIT_COMMITTER_DATE"] = "1700000000 +0000"
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True, env=env)
    return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()


def _make_keypair(root: Path, *, key_id: str, trust_name: str) -> tuple[Path, Path]:
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
    trust = root / trust_name
    trust.mkdir()
    if os.name != "nt":
        trust.chmod(0o755)
    public_path = trust / f"{key_id}.pem"
    public_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    if os.name != "nt":
        public_path.chmod(0o644)
    return private_path, trust


@pytest.fixture(scope="module")
def trusted_bundles(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, Path, Path]:
    root = tmp_path_factory.mktemp("artifact-registry")
    repo = root / "repo"
    repo.mkdir()
    _write_project(repo)
    commit = _commit_repo(repo)

    package_bundle = root / "package"
    build_bundle(repo=repo, commit=commit, output=package_bundle, builder_python=sys.executable)
    package_private, package_trust = _make_keypair(
        root / "package-keys",
        key_id="package-fixture",
        trust_name="trusted-package-keys",
    )
    sign_package_bundle(
        package_bundle,
        key_id="package-fixture",
        private_key_path=package_private,
    )

    runtime_bundle = root / "runtime"
    old_no_index = os.environ.get("PIP_NO_INDEX")
    os.environ["PIP_NO_INDEX"] = "1"
    try:
        create_runtime_bundle(
            package_bundle=package_bundle,
            trusted_package_keys=package_trust,
            output=runtime_bundle,
            python_executable=sys.executable,
        )
    finally:
        if old_no_index is None:
            os.environ.pop("PIP_NO_INDEX", None)
        else:
            os.environ["PIP_NO_INDEX"] = old_no_index

    runtime_private, runtime_trust = _make_keypair(
        root / "runtime-keys",
        key_id="runtime-fixture",
        trust_name="trusted-runtime-keys",
    )
    sign_runtime_bundle(
        runtime_bundle,
        package_bundle=package_bundle,
        trusted_package_keys=package_trust,
        key_id="runtime-fixture",
        private_key_path=runtime_private,
        python_executable=sys.executable,
    )
    return package_bundle, package_trust, runtime_bundle, runtime_trust


def _publish(
    fixture: tuple[Path, Path, Path, Path],
    tmp_path: Path,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    package, package_trust, runtime, runtime_trust = fixture
    registry = init_registry((tmp_path / "registry").resolve())
    publication = publish_artifact(
        registry_root=registry,
        package_bundle=package,
        runtime_bundle=runtime,
        trusted_package_keys=package_trust,
        trusted_runtime_keys=runtime_trust,
        python_executable=sys.executable,
    )
    return registry, publication


def _make_writable(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o700 if path.is_dir() else 0o600)


def test_registry_init_requires_absolute_safe_root(tmp_path: Path) -> None:
    with pytest.raises(ArtifactRegistryError, match="absolute"):
        init_registry(Path("relative-registry"))

    registry = init_registry((tmp_path / "registry").resolve())
    assert registry == registry.resolve()
    assert (registry / OBJECTS_DIRNAME).is_dir()
    assert init_registry(registry) == registry

    if os.name != "nt":
        registry.chmod(0o775)
        with pytest.raises(ArtifactRegistryError, match="group/world writable"):
            init_registry(registry)


def test_publish_verify_and_idempotent_republish(
    trusted_bundles: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    package, package_trust, runtime, runtime_trust = trusted_bundles
    registry, publication = _publish(trusted_bundles, tmp_path)
    artifact = registry / OBJECTS_DIRNAME / publication.artifact_id

    assert len(publication.artifact_id) == 64
    assert publication.package_version == "2.3.4"
    assert publication.wheel_count == 1
    assert {item.name for item in artifact.iterdir()} == {
        PUBLICATION_FILENAME,
        PACKAGE_DIRNAME,
        RUNTIME_DIRNAME,
    }
    verified = verify_registry_artifact(
        registry_root=registry,
        artifact_id=publication.artifact_id,
        trusted_package_keys=package_trust,
        trusted_runtime_keys=runtime_trust,
    )
    repeated = publish_artifact(
        registry_root=registry,
        package_bundle=package,
        runtime_bundle=runtime,
        trusted_package_keys=package_trust,
        trusted_runtime_keys=runtime_trust,
    )
    assert verified == publication == repeated
    assert [item.name for item in (registry / OBJECTS_DIRNAME).iterdir()] == [
        publication.artifact_id
    ]

    if os.name != "nt":
        for path in [artifact, *artifact.rglob("*")]:
            assert path.stat().st_mode & 0o222 == 0


def test_registry_rejects_package_and_runtime_tampering(
    trusted_bundles: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    _, package_trust, _, runtime_trust = trusted_bundles
    registry, publication = _publish(trusted_bundles, tmp_path)
    artifact = registry / OBJECTS_DIRNAME / publication.artifact_id

    package_wheel = next((artifact / PACKAGE_DIRNAME).glob("*.whl"))
    _make_writable(artifact)
    _make_writable(artifact / PACKAGE_DIRNAME)
    _make_writable(package_wheel)
    with package_wheel.open("ab") as handle:
        handle.write(b"tampered")
    with pytest.raises((ArtifactRegistryError, PackagingError)):
        verify_registry_artifact(
            registry_root=registry,
            artifact_id=publication.artifact_id,
            trusted_package_keys=package_trust,
            trusted_runtime_keys=runtime_trust,
        )

    registry, publication = _publish(trusted_bundles, tmp_path / "runtime-case")
    artifact = registry / OBJECTS_DIRNAME / publication.artifact_id
    runtime_wheel = next((artifact / RUNTIME_DIRNAME / WHEELHOUSE_DIRNAME).glob("*.whl"))
    _make_writable(artifact)
    _make_writable(artifact / RUNTIME_DIRNAME)
    _make_writable(artifact / RUNTIME_DIRNAME / WHEELHOUSE_DIRNAME)
    _make_writable(runtime_wheel)
    with runtime_wheel.open("ab") as handle:
        handle.write(b"tampered")
    with pytest.raises((ArtifactRegistryError, RuntimeSigningError)):
        verify_registry_artifact(
            registry_root=registry,
            artifact_id=publication.artifact_id,
            trusted_package_keys=package_trust,
            trusted_runtime_keys=runtime_trust,
        )


def test_registry_rejects_publication_tamper_and_duplicate_keys(
    trusted_bundles: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    _, package_trust, _, runtime_trust = trusted_bundles
    registry, publication = _publish(trusted_bundles, tmp_path)
    artifact = registry / OBJECTS_DIRNAME / publication.artifact_id
    record = artifact / PUBLICATION_FILENAME
    _make_writable(artifact)
    _make_writable(record)
    payload = json.loads(record.read_text())
    payload["package_version"] = "9.9.9"
    record.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    if os.name != "nt":
        record.chmod(0o444)
        artifact.chmod(0o555)
    with pytest.raises(ArtifactRegistryError, match="does not match trusted bundles"):
        verify_registry_artifact(
            registry_root=registry,
            artifact_id=publication.artifact_id,
            trusted_package_keys=package_trust,
            trusted_runtime_keys=runtime_trust,
        )

    registry, publication = _publish(trusted_bundles, tmp_path / "duplicate-case")
    artifact = registry / OBJECTS_DIRNAME / publication.artifact_id
    record = artifact / PUBLICATION_FILENAME
    _make_writable(artifact)
    _make_writable(record)
    record.write_text('{"schema_version":1,"schema_version":1}')
    if os.name != "nt":
        record.chmod(0o444)
        artifact.chmod(0o555)
    with pytest.raises(ArtifactRegistryError, match="duplicate publication JSON key"):
        verify_registry_artifact(
            registry_root=registry,
            artifact_id=publication.artifact_id,
            trusted_package_keys=package_trust,
            trusted_runtime_keys=runtime_trust,
        )


def test_registry_rejects_wrong_or_revoked_runtime_key(
    trusted_bundles: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    _, package_trust, _, runtime_trust = trusted_bundles
    registry, publication = _publish(trusted_bundles, tmp_path)
    _, wrong_trust = _make_keypair(
        tmp_path / "wrong-runtime-key",
        key_id="runtime-fixture",
        trust_name="trusted-runtime-keys",
    )
    with pytest.raises(RuntimeSigningError, match="verification failed"):
        verify_registry_artifact(
            registry_root=registry,
            artifact_id=publication.artifact_id,
            trusted_package_keys=package_trust,
            trusted_runtime_keys=wrong_trust,
        )

    revoked = tmp_path / "revoked-runtime-trust"
    shutil.copytree(runtime_trust, revoked)
    (revoked / "runtime-fixture.pem").unlink()
    with pytest.raises(RuntimeSigningError, match="trusted runtime public key"):
        verify_registry_artifact(
            registry_root=registry,
            artifact_id=publication.artifact_id,
            trusted_package_keys=package_trust,
            trusted_runtime_keys=revoked,
        )


def test_registry_rejects_invalid_identity_and_writable_tree(
    trusted_bundles: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    _, package_trust, _, runtime_trust = trusted_bundles
    registry, publication = _publish(trusted_bundles, tmp_path)
    with pytest.raises(ArtifactRegistryError, match="invalid artifact ID"):
        verify_registry_artifact(
            registry_root=registry,
            artifact_id="../escape",
            trusted_package_keys=package_trust,
            trusted_runtime_keys=runtime_trust,
        )

    artifact = registry / OBJECTS_DIRNAME / publication.artifact_id
    _make_writable(artifact)
    with pytest.raises(ArtifactRegistryError, match="read-only"):
        verify_registry_artifact(
            registry_root=registry,
            artifact_id=publication.artifact_id,
            trusted_package_keys=package_trust,
            trusted_runtime_keys=runtime_trust,
        )


def test_artifact_registry_cli(
    trusted_bundles: tuple[Path, Path, Path, Path], tmp_path: Path, capsys
) -> None:
    package, package_trust, runtime, runtime_trust = trusted_bundles
    registry = (tmp_path / "registry").resolve()
    assert main(["init", "--root", str(registry), "--json"]) == 0
    capsys.readouterr()

    common = [
        "--root",
        str(registry),
        "--trusted-package-keys",
        str(package_trust),
        "--trusted-runtime-keys",
        str(runtime_trust),
    ]
    assert (
        main(
            [
                "publish",
                *common,
                "--package-bundle",
                str(package),
                "--runtime-bundle",
                str(runtime),
                "--json",
            ]
        )
        == 0
    )
    published = json.loads(capsys.readouterr().out)
    assert (
        main(
            [
                "verify",
                *common,
                "--artifact-id",
                published["artifact_id"],
                "--json",
            ]
        )
        == 0
    )
    verified = json.loads(capsys.readouterr().out)
    assert verified["artifact_id"] == published["artifact_id"]


def test_registry_trust_anchors_must_be_external(
    trusted_bundles: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    package, package_trust, runtime, runtime_trust = trusted_bundles
    registry = init_registry((tmp_path / "registry").resolve())
    inside_package_trust = registry / "trusted-package-keys"
    shutil.copytree(package_trust, inside_package_trust)
    with pytest.raises(ArtifactRegistryError, match="outside the registry"):
        publish_artifact(
            registry_root=registry,
            package_bundle=package,
            runtime_bundle=runtime,
            trusted_package_keys=inside_package_trust,
            trusted_runtime_keys=runtime_trust,
        )

    publication = publish_artifact(
        registry_root=registry,
        package_bundle=package,
        runtime_bundle=runtime,
        trusted_package_keys=package_trust,
        trusted_runtime_keys=runtime_trust,
    )
    inside_runtime_trust = registry / "trusted-runtime-keys"
    shutil.copytree(runtime_trust, inside_runtime_trust)
    with pytest.raises(ArtifactRegistryError, match="outside the registry"):
        verify_registry_artifact(
            registry_root=registry,
            artifact_id=publication.artifact_id,
            trusted_package_keys=package_trust,
            trusted_runtime_keys=inside_runtime_trust,
        )

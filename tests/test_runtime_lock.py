from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from remote_mcp_commander.package import build_bundle
from remote_mcp_commander.package_signing import sign_package_bundle
from remote_mcp_commander.runtime_lock import (
    RUNTIME_LOCK_FILENAME,
    RUNTIME_SIGNATURE_FILENAME,
    WHEELHOUSE_DIRNAME,
    RuntimeLockError,
    create_runtime_bundle,
    main,
    verify_runtime_bundle,
)
from remote_mcp_commander.runtime_signing import (
    SIGNATURE_DOMAIN as RUNTIME_SIGNATURE_DOMAIN,
)
from remote_mcp_commander.runtime_signing import (
    RuntimeSigningError,
    load_runtime_signature,
    sign_runtime_bundle,
    verify_signed_runtime_bundle,
)


def _write_project(root: Path) -> None:
    package = root / "src" / "remote_mcp_commander"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "1.2.3"\n')
    (package / "protocol.py").write_text("PROTOCOL_MIN_SUPPORTED = 1\nPROTOCOL_MAX_SUPPORTED = 1\n")
    (root / "README.md").write_text("# runtime fixture\n")
    (root / "pyproject.toml").write_text(
        "\n".join(
            [
                "[build-system]",
                'requires = ["setuptools==84.0.0"]',
                'build-backend = "setuptools.build_meta"',
                "",
                "[project]",
                'name = "remote-mcp-commander"',
                'version = "1.2.3"',
                'description = "runtime lock fixture"',
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


def _make_keypair(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True)
    private = Ed25519PrivateKey.generate()
    private_path = root / "package-private.pem"
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
    public_path = trusted / "package-fixture.pem"
    public_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    if os.name != "nt":
        public_path.chmod(0o644)
    return private_path, trusted


def _build_fixture(root: Path) -> tuple[Path, Path, Path]:
    repo = root / "repo"
    repo.mkdir()
    _write_project(repo)
    commit = _commit_repo(repo)
    package_bundle = root / "package-bundle"
    build_bundle(repo=repo, commit=commit, output=package_bundle, builder_python=sys.executable)
    private_key, trusted = _make_keypair(root / "keys")
    sign_package_bundle(
        package_bundle,
        key_id="package-fixture",
        private_key_path=private_key,
    )
    runtime_bundle = root / "runtime-bundle"
    old_no_index = os.environ.get("PIP_NO_INDEX")
    os.environ["PIP_NO_INDEX"] = "1"
    try:
        create_runtime_bundle(
            package_bundle=package_bundle,
            trusted_package_keys=trusted,
            output=runtime_bundle,
            python_executable=sys.executable,
        )
    finally:
        if old_no_index is None:
            os.environ.pop("PIP_NO_INDEX", None)
        else:
            os.environ["PIP_NO_INDEX"] = old_no_index
    return package_bundle, trusted, runtime_bundle


@pytest.fixture(scope="module")
def runtime_fixture(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    return _build_fixture(tmp_path_factory.mktemp("runtime-lock"))


def _copy_fixture(fixture: tuple[Path, Path, Path], tmp_path: Path) -> tuple[Path, Path, Path]:
    package, trusted, runtime = fixture
    package_copy = tmp_path / "package"
    trust_copy = tmp_path / "trust"
    runtime_copy = tmp_path / "runtime"
    shutil.copytree(package, package_copy)
    shutil.copytree(trusted, trust_copy)
    shutil.copytree(runtime, runtime_copy)
    return package_copy, trust_copy, runtime_copy


def test_runtime_lock_round_trip(runtime_fixture: tuple[Path, Path, Path]) -> None:
    package, trusted, runtime = runtime_fixture
    lock = verify_runtime_bundle(
        runtime_bundle=runtime,
        package_bundle=package,
        trusted_package_keys=trusted,
        python_executable=sys.executable,
    )
    assert lock.package_version == "1.2.3"
    assert len(lock.wheels) == 1
    assert lock.wheels[0].normalized_name == "remote-mcp-commander"


def test_runtime_lock_rejects_wheel_tamper(
    runtime_fixture: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    package, trusted, runtime = _copy_fixture(runtime_fixture, tmp_path)
    wheel = next((runtime / WHEELHOUSE_DIRNAME).glob("*.whl"))
    with wheel.open("ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(RuntimeLockError, match="wheelhouse does not match"):
        verify_runtime_bundle(
            runtime_bundle=runtime,
            package_bundle=package,
            trusted_package_keys=trusted,
        )


def test_runtime_lock_rejects_extra_wheel(
    runtime_fixture: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    package, trusted, runtime = _copy_fixture(runtime_fixture, tmp_path)
    wheel = next((runtime / WHEELHOUSE_DIRNAME).glob("*.whl"))
    shutil.copyfile(wheel, wheel.with_name("extra-1.0-py3-none-any.whl"))
    with pytest.raises(RuntimeLockError):
        verify_runtime_bundle(
            runtime_bundle=runtime,
            package_bundle=package,
            trusted_package_keys=trusted,
        )


def test_runtime_lock_rejects_writable_wheelhouse(
    runtime_fixture: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    package, trusted, runtime = _copy_fixture(runtime_fixture, tmp_path)
    if os.name == "nt":
        pytest.skip("POSIX permission test")
    (runtime / WHEELHOUSE_DIRNAME).chmod(0o775)
    with pytest.raises(RuntimeLockError, match="group/world writable"):
        verify_runtime_bundle(
            runtime_bundle=runtime,
            package_bundle=package,
            trusted_package_keys=trusted,
        )


def test_runtime_lock_rejects_target_change(
    runtime_fixture: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    package, trusted, runtime = _copy_fixture(runtime_fixture, tmp_path)
    path = runtime / RUNTIME_LOCK_FILENAME
    if os.name != "nt":
        path.chmod(0o644)
    payload = json.loads(path.read_text())
    payload["target"]["platform_tag"] = "other-linux-x86_64"
    path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    if os.name != "nt":
        path.chmod(0o444)
    with pytest.raises(RuntimeLockError, match="target does not match"):
        verify_runtime_bundle(
            runtime_bundle=runtime,
            package_bundle=package,
            trusted_package_keys=trusted,
        )


def test_runtime_lock_rejects_package_manifest_change(
    runtime_fixture: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    package, trusted, runtime = _copy_fixture(runtime_fixture, tmp_path)
    path = runtime / RUNTIME_LOCK_FILENAME
    if os.name != "nt":
        path.chmod(0o644)
    payload = json.loads(path.read_text())
    payload["package_manifest_sha256"] = "0" * 64
    path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    if os.name != "nt":
        path.chmod(0o444)
    with pytest.raises(RuntimeLockError, match="different package manifest"):
        verify_runtime_bundle(
            runtime_bundle=runtime,
            package_bundle=package,
            trusted_package_keys=trusted,
        )


def test_runtime_cli_verify(runtime_fixture: tuple[Path, Path, Path], capsys) -> None:
    package, trusted, runtime = runtime_fixture
    assert (
        main(
            [
                "verify",
                "--runtime-bundle",
                str(runtime),
                "--package-bundle",
                str(package),
                "--trusted-package-keys",
                str(trusted),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["wheels"]) == 1


def test_runtime_lock_file_must_be_read_only(
    runtime_fixture: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    package, trusted, runtime = _copy_fixture(runtime_fixture, tmp_path)
    if os.name == "nt":
        pytest.skip("POSIX permission test")
    (runtime / RUNTIME_LOCK_FILENAME).chmod(0o644)
    with pytest.raises(RuntimeLockError, match="read-only"):
        verify_runtime_bundle(
            runtime_bundle=runtime,
            package_bundle=package,
            trusted_package_keys=trusted,
        )


def _make_runtime_keypair(root: Path, *, key_id: str = "runtime-2026") -> tuple[Path, Path]:
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
    trusted = root / "trusted-runtime-keys"
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


def test_runtime_signature_round_trip_and_plain_verify(
    runtime_fixture: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    package, package_trust, runtime = _copy_fixture(runtime_fixture, tmp_path)
    private_key, runtime_trust = _make_runtime_keypair(tmp_path / "runtime-keys")
    signature = sign_runtime_bundle(
        runtime,
        package_bundle=package,
        trusted_package_keys=package_trust,
        key_id="runtime-2026",
        private_key_path=private_key,
    )

    plain_lock = verify_runtime_bundle(
        runtime_bundle=runtime,
        package_bundle=package,
        trusted_package_keys=package_trust,
    )
    trusted_lock, trusted_signature = verify_signed_runtime_bundle(
        runtime,
        package_bundle=package,
        trusted_package_keys=package_trust,
        trusted_runtime_keys=runtime_trust,
    )

    assert trusted_lock == plain_lock
    assert trusted_signature == signature


def test_runtime_signature_is_domain_separated(
    runtime_fixture: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    package, package_trust, runtime = _copy_fixture(runtime_fixture, tmp_path)
    private_key, runtime_trust = _make_runtime_keypair(tmp_path / "runtime-keys")
    signature = sign_runtime_bundle(
        runtime,
        package_bundle=package,
        trusted_package_keys=package_trust,
        key_id="runtime-2026",
        private_key_path=private_key,
    )
    public = serialization.load_pem_public_key((runtime_trust / "runtime-2026.pem").read_bytes())
    raw_signature = base64.b64decode(signature.signature)
    lock_bytes = (runtime / RUNTIME_LOCK_FILENAME).read_bytes()

    with pytest.raises(InvalidSignature):
        public.verify(raw_signature, lock_bytes)
    public.verify(raw_signature, RUNTIME_SIGNATURE_DOMAIN + lock_bytes)


def test_runtime_signature_rejects_wrong_key_and_revocation(
    runtime_fixture: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    package, package_trust, runtime = _copy_fixture(runtime_fixture, tmp_path)
    private_key, runtime_trust = _make_runtime_keypair(tmp_path / "runtime-keys")
    sign_runtime_bundle(
        runtime,
        package_bundle=package,
        trusted_package_keys=package_trust,
        key_id="runtime-2026",
        private_key_path=private_key,
    )
    _, wrong_trust = _make_runtime_keypair(tmp_path / "wrong-keys")
    with pytest.raises(RuntimeSigningError, match="verification failed"):
        verify_signed_runtime_bundle(
            runtime,
            package_bundle=package,
            trusted_package_keys=package_trust,
            trusted_runtime_keys=wrong_trust,
        )

    (runtime_trust / "runtime-2026.pem").unlink()
    with pytest.raises(RuntimeSigningError, match="trusted runtime public key"):
        verify_signed_runtime_bundle(
            runtime,
            package_bundle=package,
            trusted_package_keys=package_trust,
            trusted_runtime_keys=runtime_trust,
        )


def test_runtime_signature_rejects_lock_and_wheel_tamper(
    runtime_fixture: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    package, package_trust, runtime = _copy_fixture(runtime_fixture, tmp_path)
    private_key, runtime_trust = _make_runtime_keypair(tmp_path / "runtime-keys")
    sign_runtime_bundle(
        runtime,
        package_bundle=package,
        trusted_package_keys=package_trust,
        key_id="runtime-2026",
        private_key_path=private_key,
    )
    lock_path = runtime / RUNTIME_LOCK_FILENAME
    if os.name != "nt":
        lock_path.chmod(0o600)
    lock_path.write_bytes(lock_path.read_bytes() + b" ")
    if os.name != "nt":
        lock_path.chmod(0o444)
    with pytest.raises(RuntimeSigningError, match="different lock"):
        verify_signed_runtime_bundle(
            runtime,
            package_bundle=package,
            trusted_package_keys=package_trust,
            trusted_runtime_keys=runtime_trust,
        )

    package, package_trust, runtime = _copy_fixture(runtime_fixture, tmp_path / "wheel-case")
    private_key, runtime_trust = _make_runtime_keypair(tmp_path / "wheel-keys")
    sign_runtime_bundle(
        runtime,
        package_bundle=package,
        trusted_package_keys=package_trust,
        key_id="runtime-2026",
        private_key_path=private_key,
    )
    wheel = next((runtime / WHEELHOUSE_DIRNAME).glob("*.whl"))
    with wheel.open("ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(RuntimeLockError, match="wheelhouse does not match"):
        verify_signed_runtime_bundle(
            runtime,
            package_bundle=package,
            trusted_package_keys=package_trust,
            trusted_runtime_keys=runtime_trust,
        )


def test_runtime_signing_keys_stay_outside_bundle(
    runtime_fixture: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    package, package_trust, runtime = _copy_fixture(runtime_fixture, tmp_path)
    private_key, runtime_trust = _make_runtime_keypair(tmp_path / "runtime-keys")
    inside_private = runtime / "private.pem"
    inside_private.write_bytes(private_key.read_bytes())
    if os.name != "nt":
        inside_private.chmod(0o600)
    with pytest.raises(RuntimeSigningError, match="outside the bundle"):
        sign_runtime_bundle(
            runtime,
            package_bundle=package,
            trusted_package_keys=package_trust,
            key_id="runtime-2026",
            private_key_path=inside_private,
        )
    inside_private.unlink()

    sign_runtime_bundle(
        runtime,
        package_bundle=package,
        trusted_package_keys=package_trust,
        key_id="runtime-2026",
        private_key_path=private_key,
    )
    inside_trust = runtime / "trusted-runtime-keys"
    inside_trust.mkdir()
    shutil.copyfile(runtime_trust / "runtime-2026.pem", inside_trust / "runtime-2026.pem")
    with pytest.raises(RuntimeSigningError, match="outside the bundle"):
        verify_signed_runtime_bundle(
            runtime,
            package_bundle=package,
            trusted_package_keys=package_trust,
            trusted_runtime_keys=inside_trust,
        )


def test_runtime_signature_and_key_permissions_are_enforced(
    runtime_fixture: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX permission test")
    package, package_trust, runtime = _copy_fixture(runtime_fixture, tmp_path)
    private_key, runtime_trust = _make_runtime_keypair(tmp_path / "runtime-keys")
    private_key.chmod(0o644)
    with pytest.raises(RuntimeSigningError, match="unsafe file permissions"):
        sign_runtime_bundle(
            runtime,
            package_bundle=package,
            trusted_package_keys=package_trust,
            key_id="runtime-2026",
            private_key_path=private_key,
        )
    private_key.chmod(0o600)
    sign_runtime_bundle(
        runtime,
        package_bundle=package,
        trusted_package_keys=package_trust,
        key_id="runtime-2026",
        private_key_path=private_key,
    )
    signature_path = runtime / RUNTIME_SIGNATURE_FILENAME
    signature_path.chmod(0o644)
    with pytest.raises(RuntimeSigningError, match="unsafe file permissions"):
        load_runtime_signature(runtime)
    signature_path.chmod(0o444)
    runtime_trust.chmod(0o775)
    with pytest.raises(RuntimeSigningError, match="group/world writable"):
        verify_signed_runtime_bundle(
            runtime,
            package_bundle=package,
            trusted_package_keys=package_trust,
            trusted_runtime_keys=runtime_trust,
        )


def test_duplicate_runtime_signature_keys_are_rejected(
    runtime_fixture: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    package, package_trust, runtime = _copy_fixture(runtime_fixture, tmp_path)
    private_key, _ = _make_runtime_keypair(tmp_path / "runtime-keys")
    sign_runtime_bundle(
        runtime,
        package_bundle=package,
        trusted_package_keys=package_trust,
        key_id="runtime-2026",
        private_key_path=private_key,
    )
    signature_path = runtime / RUNTIME_SIGNATURE_FILENAME
    if os.name != "nt":
        signature_path.chmod(0o600)
    signature_path.write_text('{"schema_version":1,"schema_version":1}')
    if os.name != "nt":
        signature_path.chmod(0o444)
    with pytest.raises(RuntimeSigningError, match="duplicate JSON keys"):
        load_runtime_signature(runtime)


def test_runtime_cli_sign_and_verify_trusted(
    runtime_fixture: tuple[Path, Path, Path], tmp_path: Path, capsys
) -> None:
    package, package_trust, runtime = _copy_fixture(runtime_fixture, tmp_path)
    private_key, runtime_trust = _make_runtime_keypair(tmp_path / "runtime-keys")
    common = [
        "--runtime-bundle",
        str(runtime),
        "--package-bundle",
        str(package),
        "--trusted-package-keys",
        str(package_trust),
    ]
    assert (
        main(["sign", *common, "--key-id", "runtime-2026", "--private-key", str(private_key)]) == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "verify-trusted",
                *common,
                "--trusted-runtime-keys",
                str(runtime_trust),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["signature"]["key_id"] == "runtime-2026"

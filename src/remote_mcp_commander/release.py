from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from remote_mcp_commander.release_integrity import (
    IntegrityError,
    load_manifest,
    seal_release,
    verify_release,
)
from remote_mcp_commander.release_signing import (
    SigningError,
    load_signature,
    sign_release,
    verify_signed_release,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LINK_NAMES = {"current", "previous"}
SYSTEMCTL_PATH = Path("/usr/bin/systemctl")
NATIVE_STACK_UNITS = (
    "remote-mcp-gateway.service",
    "remote-mcp-server.service",
)
GATEWAY_HEALTH_URL = "http://127.0.0.1:8765/healthz"
NATIVE_RELEASE_ROOT = Path("/opt/remote-mcp-commander")


@dataclass(frozen=True)
class ReleaseStatus:
    root: str
    current: str | None
    previous: str | None
    available: list[str]
    sealed: list[str]
    signed: list[str]


class ReleaseError(RuntimeError):
    pass


class HealthRollbackError(ReleaseError):
    def __init__(
        self,
        message: str,
        *,
        rolled_back: bool,
        recovery_healthy: bool,
    ) -> None:
        super().__init__(message)
        self.rolled_back = rolled_back
        self.recovery_healthy = recovery_healthy


def _validate_root(raw_root: str) -> Path:
    root = Path(raw_root).expanduser()
    if not root.is_absolute():
        raise ReleaseError("release root must be an absolute path")
    if root.is_symlink():
        raise ReleaseError("release root must not be a symlink")
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise ReleaseError("release root does not exist") from exc
    if not root.is_dir():
        raise ReleaseError("release root must be a directory")

    releases = root / "releases"
    if releases.is_symlink():
        raise ReleaseError("releases directory must not be a symlink")
    if not releases.is_dir():
        raise ReleaseError("release root must contain a releases directory")
    return root


def _validate_release_id(release_id: str) -> None:
    if release_id in _LINK_NAMES or not RELEASE_ID_RE.fullmatch(release_id):
        raise ReleaseError("invalid release ID")


def _release_dir(root: Path, release_id: str) -> Path:
    _validate_release_id(release_id)
    candidate = root / "releases" / release_id
    if candidate.is_symlink():
        raise ReleaseError("release directory must not be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ReleaseError(f"release does not exist: {release_id}") from exc
    if not resolved.is_dir() or resolved.parent != (root / "releases").resolve(strict=True):
        raise ReleaseError("release must be a direct child of the releases directory")

    doctor = resolved / ".venv" / "bin" / "remote-mcp-doctor"
    if doctor.is_symlink() or not doctor.is_file() or not os.access(doctor, os.X_OK):
        raise ReleaseError("release is missing an executable .venv/bin/remote-mcp-doctor")
    return resolved


def _read_release_link(root: Path, name: str) -> str | None:
    link = root / name
    if not link.exists() and not link.is_symlink():
        return None
    if not link.is_symlink():
        raise ReleaseError(f"{name} must be a symlink")
    raw_target = os.readlink(link)
    target = Path(raw_target)
    if target.is_absolute():
        raise ReleaseError(f"{name} must use a relative release link")
    expected_prefix = Path("releases")
    if target.parent != expected_prefix:
        raise ReleaseError(f"{name} points outside the releases directory")
    release_id = target.name
    _release_dir(root, release_id)
    return release_id


def _replace_link(root: Path, name: str, release_id: str) -> None:
    _release_dir(root, release_id)
    temp = root / f".{name}.tmp.{os.getpid()}"
    try:
        if temp.exists() or temp.is_symlink():
            temp.unlink()
        os.symlink(str(Path("releases") / release_id), temp)
        os.replace(temp, root / name)
    finally:
        if temp.exists() or temp.is_symlink():
            temp.unlink()


@contextmanager
def _release_lock(root: Path) -> Iterator[None]:
    path = root / ".release.lock"
    flags = os.O_CREAT | os.O_RDWR | int(getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path, flags, 0o600)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def status(root: Path) -> ReleaseStatus:
    available = []
    sealed = []
    signed = []
    for child in sorted((root / "releases").iterdir(), key=lambda item: item.name):
        if not RELEASE_ID_RE.fullmatch(child.name):
            continue
        try:
            _release_dir(root, child.name)
        except ReleaseError:
            continue
        available.append(child.name)
        try:
            load_manifest(child)
        except IntegrityError:
            pass
        else:
            sealed.append(child.name)
            try:
                load_signature(child)
            except SigningError:
                pass
            else:
                signed.append(child.name)
        if len(available) >= 1000:
            break
    return ReleaseStatus(
        str(root),
        _read_release_link(root, "current"),
        _read_release_link(root, "previous"),
        available,
        sealed,
        signed,
    )


def _run_systemctl(arguments: list[str], *, check: bool) -> subprocess.CompletedProcess[bytes]:
    if (
        SYSTEMCTL_PATH.is_symlink()
        or not SYSTEMCTL_PATH.is_file()
        or not os.access(SYSTEMCTL_PATH, os.X_OK)
    ):
        raise ReleaseError("trusted systemctl executable is unavailable")
    try:
        return subprocess.run(
            [str(SYSTEMCTL_PATH), *arguments],
            check=check,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ReleaseError("native stack service control failed") from exc


def _restart_native_stack() -> None:
    _run_systemctl(["restart", *NATIVE_STACK_UNITS, "--no-pager"], check=True)


def _native_stack_healthy() -> bool:
    try:
        service_state = _run_systemctl(
            ["is-active", "--quiet", *NATIVE_STACK_UNITS],
            check=False,
        )
        if service_state.returncode != 0:
            return False
        with httpx.Client(
            timeout=httpx.Timeout(2.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = client.get(GATEWAY_HEALTH_URL)
        return response.status_code == 200 and response.json() == {"status": "ok"}
    except (ReleaseError, httpx.HTTPError, ValueError):
        return False


def _wait_for_native_stack(
    *,
    timeout_seconds: float,
    interval_seconds: float,
) -> int | None:
    if not 1.0 <= timeout_seconds <= 300.0:
        raise ReleaseError("health timeout must be between 1 and 300 seconds")
    if not 0.1 <= interval_seconds <= min(10.0, timeout_seconds):
        raise ReleaseError("health interval must be between 0.1 seconds and the timeout")
    deadline = time.monotonic() + timeout_seconds
    attempts = 0
    while True:
        attempts += 1
        if _native_stack_healthy():
            return attempts
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(interval_seconds, remaining))


def activate_checked(
    root: Path,
    release_id: str,
    *,
    trusted_keys_dir: Path | None = None,
    timeout_seconds: float = 30.0,
    interval_seconds: float = 0.5,
) -> ReleaseStatus:
    if root.resolve(strict=True) != NATIVE_RELEASE_ROOT:
        raise ReleaseError("checked activation requires the native deployment root")
    candidate = _release_dir(root, release_id)
    trust = trusted_keys_dir or root / "trusted-release-keys"
    if not 1.0 <= timeout_seconds <= 300.0:
        raise ReleaseError("health timeout must be between 1 and 300 seconds")
    if not 0.1 <= interval_seconds <= min(10.0, timeout_seconds):
        raise ReleaseError("health interval must be between 0.1 seconds and the timeout")

    with _release_lock(root):
        verify_signed_release(candidate, trusted_keys_dir=trust)
        original = _read_release_link(root, "current")
        if original is None:
            raise ReleaseError("checked activation requires an existing current release")
        if original == release_id:
            raise ReleaseError("checked activation requires a different candidate release")
        original_dir = _release_dir(root, original)
        verify_signed_release(original_dir, trusted_keys_dir=trust)

        _replace_link(root, "previous", original)
        _replace_link(root, "current", release_id)
        failure = "candidate health check timed out"
        try:
            _restart_native_stack()
            if (
                _wait_for_native_stack(
                    timeout_seconds=timeout_seconds,
                    interval_seconds=interval_seconds,
                )
                is not None
            ):
                return status(root)
        except ReleaseError as exc:
            failure = str(exc)

        try:
            verify_signed_release(original_dir, trusted_keys_dir=trust)
            _replace_link(root, "current", original)
            _replace_link(root, "previous", release_id)
        except (OSError, ReleaseError, IntegrityError, SigningError) as exc:
            raise HealthRollbackError(
                f"{failure}; automatic rollback could not restore the previous release",
                rolled_back=False,
                recovery_healthy=False,
            ) from exc

        recovery_healthy = False
        try:
            _restart_native_stack()
            recovery_healthy = (
                _wait_for_native_stack(
                    timeout_seconds=timeout_seconds,
                    interval_seconds=interval_seconds,
                )
                is not None
            )
        except ReleaseError:
            recovery_healthy = False

        if not recovery_healthy:
            raise HealthRollbackError(
                f"{failure}; previous release was restored but failed its recovery health check",
                rolled_back=True,
                recovery_healthy=False,
            )
        raise HealthRollbackError(
            f"{failure}; previous release was restored and is healthy",
            rolled_back=True,
            recovery_healthy=True,
        )


def activate(
    root: Path,
    release_id: str,
    *,
    trusted_keys_dir: Path | None = None,
) -> ReleaseStatus:
    candidate = _release_dir(root, release_id)
    trust = trusted_keys_dir or root / "trusted-release-keys"
    with _release_lock(root):
        verify_signed_release(candidate, trusted_keys_dir=trust)
        current = _read_release_link(root, "current")
        if current == release_id:
            return status(root)
        if current is not None:
            _replace_link(root, "previous", current)
        _replace_link(root, "current", release_id)
        return status(root)


def rollback(root: Path, *, trusted_keys_dir: Path | None = None) -> ReleaseStatus:
    trust = trusted_keys_dir or root / "trusted-release-keys"
    with _release_lock(root):
        current = _read_release_link(root, "current")
        previous = _read_release_link(root, "previous")
        if previous is None:
            raise ReleaseError("no previous release is available")
        verify_signed_release(_release_dir(root, previous), trusted_keys_dir=trust)
        _replace_link(root, "current", previous)
        if current is not None:
            _replace_link(root, "previous", current)
        return status(root)


def _print_status(result: ReleaseStatus, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(asdict(result), sort_keys=True))
        return
    print(f"root: {result.root}")
    print(f"current: {result.current or '-'}")
    print(f"previous: {result.previous or '-'}")
    print("available: " + (", ".join(result.available) or "-"))
    print("sealed: " + (", ".join(result.sealed) or "-"))
    print("signed: " + (", ".join(result.signed) or "-"))


def _print_manifest(manifest, *, json_output: bool) -> None:
    payload = {
        "release_id": manifest.release_id,
        "package_version": manifest.package_version,
        "commit_sha": manifest.commit_sha,
        "protocol_min": manifest.protocol_min,
        "protocol_max": manifest.protocol_max,
        "entry_count": len(manifest.entries),
        "total_bytes": manifest.total_bytes,
        "tree_sha256": manifest.tree_sha256,
    }
    if json_output:
        print(json.dumps(payload, sort_keys=True))
        return
    for key, value in payload.items():
        print(f"{key}: {value}")


def _print_verified(manifest, signature, *, json_output: bool) -> None:
    payload = {
        "release_id": manifest.release_id,
        "package_version": manifest.package_version,
        "commit_sha": manifest.commit_sha,
        "protocol_min": manifest.protocol_min,
        "protocol_max": manifest.protocol_max,
        "tree_sha256": manifest.tree_sha256,
        "signing_key_id": signature.key_id,
        "manifest_sha256": signature.manifest_sha256,
    }
    if json_output:
        print(json.dumps(payload, sort_keys=True))
        return
    for key, value in payload.items():
        print(f"{key}: {value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage atomic Remote MCP Commander releases")
    parser.add_argument("--root", required=True, help="absolute deployment root")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument(
        "--trusted-keys-dir",
        help="trusted Ed25519 public-key directory (default: <root>/trusted-release-keys)",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("status")
    seal_parser = subparsers.add_parser("seal")
    seal_parser.add_argument("release_id")
    seal_parser.add_argument("--commit-sha", required=True)
    integrity_parser = subparsers.add_parser("verify-integrity")
    integrity_parser.add_argument("release_id")
    sign_parser = subparsers.add_parser("sign")
    sign_parser.add_argument("release_id")
    sign_parser.add_argument("--key-id", required=True)
    sign_parser.add_argument("--private-key", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("release_id")
    activate_parser = subparsers.add_parser("activate")
    activate_parser.add_argument("release_id")
    checked_parser = subparsers.add_parser("activate-checked")
    checked_parser.add_argument("release_id")
    checked_parser.add_argument("--timeout-seconds", type=float, default=30.0)
    checked_parser.add_argument("--interval-seconds", type=float, default=0.5)
    subparsers.add_parser("rollback")
    args = parser.parse_args(argv)

    try:
        root = _validate_root(args.root)
        trust = (
            Path(args.trusted_keys_dir).expanduser()
            if args.trusted_keys_dir
            else root / "trusted-release-keys"
        )
        if args.action == "status":
            result = status(root)
        elif args.action == "seal":
            manifest = seal_release(_release_dir(root, args.release_id), commit_sha=args.commit_sha)
            _print_manifest(manifest, json_output=args.json_output)
            return 0
        elif args.action == "verify-integrity":
            manifest = verify_release(_release_dir(root, args.release_id))
            _print_manifest(manifest, json_output=args.json_output)
            return 0
        elif args.action == "sign":
            signature = sign_release(
                _release_dir(root, args.release_id),
                key_id=args.key_id,
                private_key_path=Path(args.private_key),
            )
            manifest = verify_release(_release_dir(root, args.release_id))
            _print_verified(manifest, signature, json_output=args.json_output)
            return 0
        elif args.action == "verify":
            manifest, signature = verify_signed_release(
                _release_dir(root, args.release_id), trusted_keys_dir=trust
            )
            _print_verified(manifest, signature, json_output=args.json_output)
            return 0
        elif args.action == "activate":
            result = activate(root, args.release_id, trusted_keys_dir=trust)
        elif args.action == "activate-checked":
            result = activate_checked(
                root,
                args.release_id,
                trusted_keys_dir=trust,
                timeout_seconds=args.timeout_seconds,
                interval_seconds=args.interval_seconds,
            )
        else:
            result = rollback(root, trusted_keys_dir=trust)
    except HealthRollbackError as exc:
        if args.json_output:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": str(exc),
                        "rolled_back": exc.rolled_back,
                        "recovery_healthy": exc.recovery_healthy,
                    },
                    sort_keys=True,
                )
            )
        else:
            print(f"release health error: {exc}")
        return 1
    except (OSError, ReleaseError, IntegrityError, SigningError) as exc:
        if args.json_output:
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        else:
            print(f"release error: {exc}")
        return 1

    _print_status(result, json_output=args.json_output)
    return 0


def run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    run()

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from remote_mcp_commander.release_integrity import (
    IntegrityError,
    load_manifest,
    seal_release,
    verify_release,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LINK_NAMES = {"current", "previous"}


@dataclass(frozen=True)
class ReleaseStatus:
    root: str
    current: str | None
    previous: str | None
    available: list[str]
    sealed: list[str]


class ReleaseError(RuntimeError):
    pass


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
        if len(available) >= 1000:
            break
    return ReleaseStatus(
        str(root),
        _read_release_link(root, "current"),
        _read_release_link(root, "previous"),
        available,
        sealed,
    )


def activate(root: Path, release_id: str) -> ReleaseStatus:
    candidate = _release_dir(root, release_id)
    with _release_lock(root):
        verify_release(candidate)
        current = _read_release_link(root, "current")
        if current == release_id:
            return status(root)
        if current is not None:
            _replace_link(root, "previous", current)
        _replace_link(root, "current", release_id)
        return status(root)


def rollback(root: Path) -> ReleaseStatus:
    with _release_lock(root):
        current = _read_release_link(root, "current")
        previous = _read_release_link(root, "previous")
        if previous is None:
            raise ReleaseError("no previous release is available")
        verify_release(_release_dir(root, previous))
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage atomic Remote MCP Commander releases")
    parser.add_argument("--root", required=True, help="absolute deployment root")
    parser.add_argument("--json", action="store_true", dest="json_output")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("status")
    seal_parser = subparsers.add_parser("seal")
    seal_parser.add_argument("release_id")
    seal_parser.add_argument("--commit-sha", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("release_id")
    activate_parser = subparsers.add_parser("activate")
    activate_parser.add_argument("release_id")
    subparsers.add_parser("rollback")
    args = parser.parse_args(argv)

    try:
        root = _validate_root(args.root)
        if args.action == "status":
            result = status(root)
        elif args.action == "seal":
            manifest = seal_release(_release_dir(root, args.release_id), commit_sha=args.commit_sha)
            _print_manifest(manifest, json_output=args.json_output)
            return 0
        elif args.action == "verify":
            manifest = verify_release(_release_dir(root, args.release_id))
            _print_manifest(manifest, json_output=args.json_output)
            return 0
        elif args.action == "activate":
            result = activate(root, args.release_id)
        else:
            result = rollback(root)
    except (OSError, ReleaseError, IntegrityError) as exc:
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

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from remote_mcp_commander.agent.file_ops import resolve_allowed_path
from remote_mcp_commander.protocol import GitStatusResult

TRUSTED_GIT_PATH = "/usr/bin:/bin"


def _inside_any_root(path: Path, roots: list[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _find_repo_root(raw_path: str, roots: list[Path]) -> tuple[Path | None, str | None]:
    try:
        path = resolve_allowed_path(raw_path, roots).resolve(strict=True)
    except (OSError, PermissionError, ValueError) as exc:
        return None, str(exc)
    if not path.is_dir():
        return None, "git status path must be a directory"

    current = path
    while _inside_any_root(current, roots):
        marker = current / ".git"
        if marker.is_symlink():
            return None, "symlink .git metadata is not supported"
        if marker.is_file():
            return None, "gitfile/worktree repositories are not supported"
        if marker.is_dir():
            try:
                resolved_marker = marker.resolve(strict=True)
            except OSError as exc:
                return None, str(exc)
            if not _inside_any_root(resolved_marker, roots):
                return None, "git metadata is outside configured allowed roots"
            return current, None
        if current.parent == current:
            break
        current = current.parent
    return None, "no supported git repository found inside configured allowed roots"


async def git_status(
    request_id: str,
    raw_path: str,
    *,
    roots: list[Path],
    timeout_s: float,
    max_output_bytes: int,
) -> GitStatusResult:
    repo_root, error = _find_repo_root(raw_path, roots)
    if repo_root is None:
        return GitStatusResult(request_id=request_id, rejected=True, error=error)

    git = shutil.which("git", path=TRUSTED_GIT_PATH)
    if git is None:
        return GitStatusResult(
            request_id=request_id,
            path=str(repo_root),
            rejected=True,
            error="git is not available",
        )
    args = [
        git,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "status.submoduleSummary=false",
        "-C",
        str(repo_root),
        "status",
        "--porcelain=v2",
        "--branch",
        "--untracked-files=normal",
        "--ignore-submodules=all",
    ]
    env = {
        "PATH": TRUSTED_GIT_PATH,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
    }
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except TimeoutError:
        process.kill()
        await process.wait()
        return GitStatusResult(
            request_id=request_id,
            path=str(repo_root),
            repo_root=str(repo_root),
            error="git status timed out",
        )

    if process.returncode not in (0, None):
        message = stderr.decode("utf-8", errors="replace")[:512].strip() or "git status failed"
        return GitStatusResult(
            request_id=request_id,
            path=str(repo_root),
            repo_root=str(repo_root),
            rejected=True,
            error=message,
        )

    truncated = len(stdout) > max_output_bytes
    porcelain = stdout[:max_output_bytes].decode("utf-8", errors="replace")
    branch = None
    head_oid = None
    dirty = False
    for line in porcelain.splitlines():
        if line.startswith("# branch.head "):
            branch = line.removeprefix("# branch.head ")
        elif line.startswith("# branch.oid "):
            head_oid = line.removeprefix("# branch.oid ")
        elif line and not line.startswith("#"):
            dirty = True

    return GitStatusResult(
        request_id=request_id,
        path=str(repo_root),
        repo_root=str(repo_root),
        branch=branch,
        head_oid=head_oid,
        porcelain=porcelain,
        clean=not dirty if not truncated else None,
        truncated=truncated,
    )

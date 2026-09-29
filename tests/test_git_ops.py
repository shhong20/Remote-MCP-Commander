import subprocess
from pathlib import Path

import pytest

from remote_mcp_commander.agent.git_ops import git_status


def init_repo(path: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", str(path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.mark.asyncio
async def test_git_status_reads_normal_repo_inside_allowed_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    (repo / "untracked.txt").write_text("hello\n", encoding="utf-8")

    result = await git_status(
        "git-1",
        str(repo),
        roots=[tmp_path.resolve()],
        timeout_s=2,
        max_output_bytes=4096,
    )
    assert result.rejected is False
    assert result.repo_root == str(repo.resolve())
    assert result.clean is False
    assert "? untracked.txt" in result.porcelain


@pytest.mark.asyncio
async def test_git_status_does_not_walk_above_allowed_root(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    init_repo(outer)
    allowed = outer / "allowed"
    allowed.mkdir()

    result = await git_status(
        "git-2",
        str(allowed),
        roots=[allowed.resolve()],
        timeout_s=2,
        max_output_bytes=4096,
    )
    assert result.rejected is True
    assert "no supported git repository" in (result.error or "")


@pytest.mark.asyncio
async def test_git_status_rejects_gitfile_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /tmp/outside\n", encoding="utf-8")

    result = await git_status(
        "git-3",
        str(repo),
        roots=[tmp_path.resolve()],
        timeout_s=2,
        max_output_bytes=4096,
    )
    assert result.rejected is True
    assert result.error == "gitfile/worktree repositories are not supported"


@pytest.mark.asyncio
async def test_git_status_rejects_symlink_git_metadata(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    metadata = tmp_path / "metadata"
    repo.mkdir()
    metadata.mkdir()
    (repo / ".git").symlink_to(metadata, target_is_directory=True)

    result = await git_status(
        "git-4",
        str(repo),
        roots=[tmp_path.resolve()],
        timeout_s=2,
        max_output_bytes=4096,
    )
    assert result.rejected is True
    assert result.error == "symlink .git metadata is not supported"

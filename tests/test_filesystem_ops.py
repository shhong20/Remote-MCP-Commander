from pathlib import Path

import pytest

from remote_mcp_commander.agent.filesystem_ops import (
    file_info,
    list_directory,
    list_file_roots,
)


@pytest.mark.asyncio
async def test_list_file_roots_returns_configured_resolved_roots(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    result = await list_file_roots("roots-1", [first.resolve(), second.resolve()])

    assert result.roots == [str(first.resolve()), str(second.resolve())]


@pytest.mark.asyncio
async def test_list_directory_reports_types_without_following_symlink(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "dir").mkdir()
    (root / "file.txt").write_text("abc", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (root / "link").symlink_to(outside)

    result = await list_directory(
        "list-1",
        str(root),
        roots=[root.resolve()],
        limit=20,
    )

    assert result.rejected is False
    by_name = {entry.name: entry for entry in result.entries}
    assert by_name["dir"].kind == "directory"
    assert by_name["file.txt"].kind == "file"
    assert by_name["file.txt"].size == 3
    assert by_name["link"].kind == "symlink"
    assert by_name["link"].size is None
    assert "secret" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_list_directory_is_bounded(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for index in range(4):
        (root / f"f{index}.txt").write_text(str(index), encoding="utf-8")

    result = await list_directory(
        "list-2",
        str(root),
        roots=[root.resolve()],
        limit=2,
    )

    assert len(result.entries) == 2
    assert result.truncated is True


@pytest.mark.asyncio
async def test_list_directory_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)

    result = await list_directory(
        "list-3",
        str(root / "escape"),
        roots=[root.resolve()],
        limit=20,
    )

    assert result.rejected is True
    assert "outside configured allowed roots" in (result.error or "")


@pytest.mark.asyncio
async def test_file_info_reports_symlink_without_following_target(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside.txt"
    root.mkdir()
    outside.write_text("top-secret", encoding="utf-8")
    link = root / "link"
    link.symlink_to(outside)

    result = await file_info("info-1", str(link), roots=[root.resolve()])

    assert result.rejected is False
    assert result.kind == "symlink"
    assert result.size is None
    assert "top-secret" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_file_info_rejects_parent_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (root / "escape").symlink_to(outside, target_is_directory=True)

    result = await file_info(
        "info-2",
        str(root / "escape" / "secret.txt"),
        roots=[root.resolve()],
    )

    assert result.rejected is True
    assert "outside configured allowed roots" in (result.error or "")


@pytest.mark.asyncio
async def test_file_info_rejects_relative_path(tmp_path: Path) -> None:
    result = await file_info("info-3", "relative.txt", roots=[tmp_path.resolve()])

    assert result.rejected is True
    assert result.error == "file path must be absolute"

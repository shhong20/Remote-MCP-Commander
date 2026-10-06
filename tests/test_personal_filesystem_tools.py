from pathlib import Path

import pytest

from remote_mcp_commander.agent.path_ops import mutate_path
from remote_mcp_commander.agent.search_ops import search_files


@pytest.mark.asyncio
async def test_search_files_and_content(tmp_path: Path) -> None:
    (tmp_path / "alpha.py").write_text("hello\nneedle here\n", encoding="utf-8")
    (tmp_path / "beta.txt").write_text("nothing\n", encoding="utf-8")

    files = await search_files(
        "req-files",
        str(tmp_path),
        "alpha",
        roots=[tmp_path],
        mode="files",
        file_glob=None,
        case_sensitive=False,
        max_results=10,
    )
    assert [Path(item.path).name for item in files.matches] == ["alpha.py"]

    content = await search_files(
        "req-content",
        str(tmp_path),
        "needle",
        roots=[tmp_path],
        mode="content",
        file_glob="*.py",
        case_sensitive=False,
        max_results=10,
    )
    assert len(content.matches) == 1
    assert content.matches[0].line == 2


@pytest.mark.asyncio
async def test_search_skips_binary_and_symlink_directory(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("needle", encoding="utf-8")
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    (tmp_path / "binary.bin").write_bytes(b"needle\x00payload")

    result = await search_files(
        "req",
        str(tmp_path),
        "needle",
        roots=[tmp_path],
        mode="content",
        file_glob=None,
        case_sensitive=False,
        max_results=20,
    )
    assert result.matches == []


@pytest.mark.asyncio
async def test_create_copy_move_delete_round_trip(tmp_path: Path) -> None:
    created = await mutate_path(
        "mkdir", "mkdir", str(tmp_path / "a" / "b"), roots=[tmp_path], parents=True
    )
    assert created.changed is True
    source = tmp_path / "source.txt"
    source.write_text("hello", encoding="utf-8")

    copied = await mutate_path(
        "copy",
        "copy",
        str(source),
        roots=[tmp_path],
        destination=str(tmp_path / "a" / "copy.txt"),
    )
    assert copied.changed is True
    assert (tmp_path / "a" / "copy.txt").read_text() == "hello"

    moved = await mutate_path(
        "move",
        "move",
        str(tmp_path / "a" / "copy.txt"),
        roots=[tmp_path],
        destination=str(tmp_path / "a" / "moved.txt"),
    )
    assert moved.changed is True
    deleted = await mutate_path(
        "delete", "delete", str(tmp_path / "a" / "moved.txt"), roots=[tmp_path]
    )
    assert deleted.changed is True


@pytest.mark.asyncio
async def test_delete_rejects_nonempty_directory_and_root(tmp_path: Path) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "child.txt").write_text("x", encoding="utf-8")
    nonempty = await mutate_path("delete", "delete", str(folder), roots=[tmp_path])
    assert nonempty.rejected is True
    root = await mutate_path("delete-root", "delete", str(tmp_path), roots=[tmp_path])
    assert root.rejected is True


@pytest.mark.asyncio
async def test_mutation_rejects_symlink_source_and_outside_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("x", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(source)
    symlink_result = await mutate_path("delete-link", "delete", str(link), roots=[tmp_path])
    assert symlink_result.rejected is True

    outside = tmp_path.parent / f"{tmp_path.name}-dest.txt"
    copy_result = await mutate_path(
        "copy-outside",
        "copy",
        str(source),
        roots=[tmp_path],
        destination=str(outside),
    )
    assert copy_result.rejected is True

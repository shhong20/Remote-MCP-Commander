from pathlib import Path

import pytest

from remote_mcp_commander.agent.filesystem_ops import list_directory_tree


@pytest.mark.asyncio
async def test_directory_tree_respects_depth_hidden_and_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    nested = root / "src" / "pkg"
    nested.mkdir(parents=True)
    (root / "top.txt").write_text("top")
    (root / ".hidden.txt").write_text("hidden")
    (root / "src" / "a.py").write_text("a")
    (nested / "deep.py").write_text("deep")
    (root / "linked").symlink_to(root / "src", target_is_directory=True)

    result = await list_directory_tree(
        "tree-1", str(root), roots=[tmp_path.resolve()], depth=2,
        include_hidden=False, per_directory_limit=100, max_entries=1000,
    )
    assert result.rejected is False
    by_relative = {entry.relative_path: entry for entry in result.entries}
    assert "top.txt" in by_relative
    assert "src" in by_relative
    assert "src/a.py" in by_relative
    assert "src/pkg" in by_relative
    assert "src/pkg/deep.py" not in by_relative
    assert ".hidden.txt" not in by_relative
    assert by_relative["linked"].kind == "symlink"
    assert not any(path.startswith("linked/") for path in by_relative)


@pytest.mark.asyncio
async def test_directory_tree_can_include_hidden(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / ".hidden").write_text("x")
    result = await list_directory_tree(
        "tree-2", str(root), roots=[tmp_path.resolve()], depth=1,
        include_hidden=True, per_directory_limit=100, max_entries=1000,
    )
    assert [entry.relative_path for entry in result.entries] == [".hidden"]


@pytest.mark.asyncio
async def test_directory_tree_sets_truncated_for_bounds(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for index in range(5):
        (root / f"f{index}.txt").write_text(str(index))
    per_dir = await list_directory_tree(
        "tree-3", str(root), roots=[tmp_path.resolve()], depth=1,
        include_hidden=False, per_directory_limit=2, max_entries=1000,
    )
    assert len(per_dir.entries) == 2
    assert per_dir.truncated is True
    total = await list_directory_tree(
        "tree-4", str(root), roots=[tmp_path.resolve()], depth=1,
        include_hidden=False, per_directory_limit=100, max_entries=3,
    )
    assert len(total.entries) == 3
    assert total.truncated is True


@pytest.mark.asyncio
async def test_directory_tree_rejects_outside_root(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    result = await list_directory_tree(
        "tree-5", str(outside), roots=[allowed.resolve()], depth=2,
        include_hidden=False, per_directory_limit=100, max_entries=1000,
    )
    assert result.rejected is True
    assert "outside configured allowed roots" in (result.error or "")

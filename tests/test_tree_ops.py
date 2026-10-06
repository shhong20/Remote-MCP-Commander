from pathlib import Path

import pytest

from remote_mcp_commander.agent.tree_ops import inspect_tree, mutate_tree


@pytest.mark.asyncio
async def test_tree_inspect_copy_and_delete_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "nested").mkdir()
    (source / "a.txt").write_text("alpha")
    (source / "nested" / "b.txt").write_text("beta")
    roots = [tmp_path.resolve()]
    inspected = await inspect_tree(
        "inspect", str(source), roots=roots, max_entries=100, max_total_bytes=1024
    )
    assert inspected.rejected is False
    assert inspected.entries == 3
    assert inspected.total_bytes == 9
    assert inspected.tree_sha256 is not None

    destination = tmp_path / "copy"
    copied = await mutate_tree(
        "copy",
        "copy_tree",
        str(source),
        roots=roots,
        destination=str(destination),
        expected_tree_sha256=inspected.tree_sha256,
        max_entries=100,
        max_total_bytes=1024,
    )
    assert copied.changed is True
    assert (destination / "nested" / "b.txt").read_text() == "beta"

    copy_manifest = await inspect_tree(
        "copy-inspect", str(destination), roots=roots, max_entries=100, max_total_bytes=1024
    )
    assert copy_manifest.tree_sha256 == inspected.tree_sha256
    deleted = await mutate_tree(
        "delete",
        "delete_tree",
        str(destination),
        roots=roots,
        destination=None,
        expected_tree_sha256=copy_manifest.tree_sha256 or "",
        max_entries=100,
        max_total_bytes=1024,
    )
    assert deleted.changed is True
    assert not destination.exists()


@pytest.mark.asyncio
async def test_tree_mutation_rejects_stale_fingerprint(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = source / "a.txt"
    target.write_text("before")
    roots = [tmp_path.resolve()]
    inspected = await inspect_tree(
        "inspect", str(source), roots=roots, max_entries=100, max_total_bytes=1024
    )
    target.write_text("after")
    result = await mutate_tree(
        "delete",
        "delete_tree",
        str(source),
        roots=roots,
        destination=None,
        expected_tree_sha256=inspected.tree_sha256 or "",
        max_entries=100,
        max_total_bytes=1024,
    )
    assert result.rejected is True
    assert "changed since inspection" in (result.error or "")
    assert source.exists()


@pytest.mark.asyncio
async def test_tree_ops_reject_symlinks_and_allowed_root_delete(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (source / "link.txt").symlink_to(outside)
    roots = [tmp_path.resolve()]
    inspected = await inspect_tree(
        "inspect", str(source), roots=roots, max_entries=100, max_total_bytes=1024
    )
    assert inspected.rejected is True
    assert "symlink" in (inspected.error or "")

    root_delete = await mutate_tree(
        "delete-root",
        "delete_tree",
        str(tmp_path),
        roots=roots,
        destination=None,
        expected_tree_sha256="0" * 64,
        max_entries=100,
        max_total_bytes=1024,
    )
    assert root_delete.rejected is True
    assert "allowed root" in (root_delete.error or "")


@pytest.mark.asyncio
async def test_copy_tree_rejects_destination_inside_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("alpha")
    roots = [tmp_path.resolve()]
    inspected = await inspect_tree(
        "inspect", str(source), roots=roots, max_entries=100, max_total_bytes=1024
    )
    result = await mutate_tree(
        "copy",
        "copy_tree",
        str(source),
        roots=roots,
        destination=str(source / "nested-copy"),
        expected_tree_sha256=inspected.tree_sha256 or "",
        max_entries=100,
        max_total_bytes=1024,
    )
    assert result.rejected is True
    assert "inside source" in (result.error or "")


@pytest.mark.asyncio
async def test_tree_inspection_enforces_entry_and_byte_limits(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("12345")
    (source / "b.txt").write_text("67890")
    roots = [tmp_path.resolve()]
    entries = await inspect_tree(
        "entries", str(source), roots=roots, max_entries=1, max_total_bytes=100
    )
    assert entries.rejected is True
    assert "entry limit" in (entries.error or "")
    size = await inspect_tree("size", str(source), roots=roots, max_entries=10, max_total_bytes=5)
    assert size.rejected is True
    assert "byte limit" in (size.error or "")

from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path

import pytest

from remote_mcp_commander.agent.archive_ops import (
    create_archive,
    extract_archive,
    inspect_archive,
)
from remote_mcp_commander.agent.tree_ops import inspect_tree


@pytest.mark.asyncio
async def test_zip_inspect_and_extract_round_trip(tmp_path: Path) -> None:
    archive = tmp_path / "sample.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        handle.writestr("dir/a.txt", "alpha")
        handle.writestr("b.txt", "beta")

    inspected = await inspect_archive("inspect", str(archive), roots=[tmp_path])
    assert inspected.rejected is False
    assert inspected.format == "zip"
    assert inspected.entries == 2
    assert inspected.total_uncompressed_bytes == 9
    assert {entry.path for entry in inspected.listing} == {"dir/a.txt", "b.txt"}

    destination = tmp_path / "out"
    extracted = await extract_archive(
        "extract", str(archive), str(destination), roots=[tmp_path]
    )
    assert extracted.rejected is False
    assert extracted.entries == 2
    assert (destination / "dir" / "a.txt").read_text() == "alpha"
    assert (destination / "b.txt").read_text() == "beta"


@pytest.mark.asyncio
async def test_zip_rejects_path_traversal_without_writing(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../escape.txt", "nope")

    inspected = await inspect_archive("inspect", str(archive), roots=[tmp_path])
    assert inspected.rejected is True
    assert "unsafe entry path" in (inspected.error or "")

    destination = tmp_path / "out"
    extracted = await extract_archive(
        "extract", str(archive), str(destination), roots=[tmp_path]
    )
    assert extracted.rejected is True
    assert not destination.exists()
    assert not (tmp_path.parent / "escape.txt").exists()


@pytest.mark.asyncio
async def test_tar_rejects_symlink_entry(tmp_path: Path) -> None:
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as handle:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        handle.addfile(info)

    inspected = await inspect_archive("inspect", str(archive), roots=[tmp_path])
    assert inspected.rejected is True
    assert "link or special entry" in (inspected.error or "")


@pytest.mark.asyncio
async def test_create_zip_from_inspected_tree_and_extract(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("alpha")
    nested = source / "nested"
    nested.mkdir()
    (nested / "b.bin").write_bytes(b"123456")

    tree = await inspect_tree(
        "tree",
        str(source),
        roots=[tmp_path],
        max_entries=5000,
        max_total_bytes=268_435_456,
    )
    assert tree.rejected is False
    assert tree.tree_sha256

    archive = tmp_path / "bundle.zip"
    created = await create_archive(
        "create",
        str(source),
        str(archive),
        roots=[tmp_path],
        expected_tree_sha256=tree.tree_sha256,
    )
    assert created.rejected is False
    assert created.format == "zip"
    assert created.entries == tree.entries
    assert created.total_uncompressed_bytes == tree.total_bytes
    assert archive.is_file()

    destination = tmp_path / "restored"
    extracted = await extract_archive(
        "extract", str(archive), str(destination), roots=[tmp_path]
    )
    assert extracted.rejected is False
    assert (destination / "a.txt").read_text() == "alpha"
    assert (destination / "nested" / "b.bin").read_bytes() == b"123456"


@pytest.mark.asyncio
async def test_create_tar_gz_preserves_empty_directory(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "empty").mkdir()

    tree = await inspect_tree(
        "tree",
        str(source),
        roots=[tmp_path],
        max_entries=5000,
        max_total_bytes=268_435_456,
    )
    assert tree.tree_sha256

    archive = tmp_path / "bundle.tar.gz"
    created = await create_archive(
        "create",
        str(source),
        str(archive),
        roots=[tmp_path],
        expected_tree_sha256=tree.tree_sha256,
    )
    assert created.rejected is False
    destination = tmp_path / "restored"
    extracted = await extract_archive(
        "extract", str(archive), str(destination), roots=[tmp_path]
    )
    assert extracted.rejected is False
    assert (destination / "empty").is_dir()


@pytest.mark.asyncio
async def test_create_archive_rejects_stale_tree_fingerprint(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("alpha")

    tree = await inspect_tree(
        "tree",
        str(source),
        roots=[tmp_path],
        max_entries=5000,
        max_total_bytes=268_435_456,
    )
    assert tree.tree_sha256
    (source / "a.txt").write_text("changed")

    result = await create_archive(
        "create",
        str(source),
        str(tmp_path / "bundle.zip"),
        roots=[tmp_path],
        expected_tree_sha256=tree.tree_sha256,
    )
    assert result.rejected is True
    assert "changed since inspection" in (result.error or "")
    assert not (tmp_path / "bundle.zip").exists()


@pytest.mark.asyncio
async def test_extract_rejects_existing_destination(tmp_path: Path) -> None:
    archive = tmp_path / "sample.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("a.txt", "alpha")
    destination = tmp_path / "out"
    destination.mkdir()

    result = await extract_archive(
        "extract", str(archive), str(destination), roots=[tmp_path]
    )
    assert result.rejected is True
    assert "already exists" in (result.error or "")


@pytest.mark.asyncio
async def test_create_archive_rejects_output_inside_source_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("alpha")
    tree = await inspect_tree(
        "tree",
        str(source),
        roots=[tmp_path],
        max_entries=5000,
        max_total_bytes=268_435_456,
    )
    assert tree.tree_sha256

    result = await create_archive(
        "create",
        str(source),
        str(source / "bundle.zip"),
        roots=[tmp_path],
        expected_tree_sha256=tree.tree_sha256,
    )
    assert result.rejected is True
    assert "outside source tree" in (result.error or "")
    assert not (source / "bundle.zip").exists()

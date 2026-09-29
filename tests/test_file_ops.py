import pytest

from remote_mcp_commander.agent.file_ops import (
    allowed_roots,
    file_sha256,
    read_text_file,
    resolve_allowed_path,
    write_text_file,
)


def test_resolve_allowed_path_blocks_outside_and_symlink_escape(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    roots = allowed_roots([str(root)])

    with pytest.raises(PermissionError):
        resolve_allowed_path(str(outside / "secret.txt"), roots)
    with pytest.raises(PermissionError):
        resolve_allowed_path(str(root / "escape" / "secret.txt"), roots)


@pytest.mark.asyncio
async def test_read_file_returns_chunk_hash_and_offsets(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    path = root / "note.txt"
    path.write_text("abcdefghij", encoding="utf-8")
    roots = allowed_roots([str(root)])

    result = await read_text_file(
        "req-read",
        str(path),
        roots=roots,
        offset=2,
        max_bytes=4,
        max_file_bytes=1024,
    )
    assert result.rejected is False
    assert result.content == "cdef"
    assert result.size == 10
    assert result.next_offset == 6
    assert result.eof is False
    assert result.sha256 == file_sha256(path)


@pytest.mark.asyncio
async def test_create_file_is_bounded_to_allowed_root(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    roots = allowed_roots([str(root)])
    path = root / "new.txt"

    result = await write_text_file(
        "req-write",
        str(path),
        "hello",
        roots=roots,
        overwrite=False,
        expected_sha256=None,
        max_file_bytes=1024,
    )
    assert result.rejected is False
    assert path.read_text(encoding="utf-8") == "hello"
    assert result.sha256 == file_sha256(path)


@pytest.mark.asyncio
async def test_overwrite_requires_matching_hash(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    roots = allowed_roots([str(root)])
    path = root / "existing.txt"
    path.write_text("old", encoding="utf-8")
    current_hash = file_sha256(path)

    missing_hash = await write_text_file(
        "req-missing",
        str(path),
        "new",
        roots=roots,
        overwrite=True,
        expected_sha256=None,
        max_file_bytes=1024,
    )
    assert missing_hash.rejected is True
    assert path.read_text(encoding="utf-8") == "old"

    stale_hash = await write_text_file(
        "req-stale",
        str(path),
        "new",
        roots=roots,
        overwrite=True,
        expected_sha256="0" * 64,
        max_file_bytes=1024,
    )
    assert stale_hash.rejected is True
    assert path.read_text(encoding="utf-8") == "old"

    updated = await write_text_file(
        "req-update",
        str(path),
        "new",
        roots=roots,
        overwrite=True,
        expected_sha256=current_hash,
        max_file_bytes=1024,
    )
    assert updated.rejected is False
    assert path.read_text(encoding="utf-8") == "new"


@pytest.mark.asyncio
async def test_file_size_limits_fail_closed(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    roots = allowed_roots([str(root)])
    path = root / "large.txt"
    path.write_text("x" * 20, encoding="utf-8")

    read_result = await read_text_file(
        "req-large-read",
        str(path),
        roots=roots,
        offset=0,
        max_bytes=8,
        max_file_bytes=10,
    )
    assert read_result.rejected is True

    write_result = await write_text_file(
        "req-large-write",
        str(root / "new.txt"),
        "y" * 20,
        roots=roots,
        overwrite=False,
        expected_sha256=None,
        max_file_bytes=10,
    )
    assert write_result.rejected is True


@pytest.mark.asyncio
async def test_read_preserves_utf8_character_boundaries(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    path = root / "korean.txt"
    path.write_text("가나다", encoding="utf-8")
    roots = allowed_roots([str(root)])

    first = await read_text_file(
        "utf8-1",
        str(path),
        roots=roots,
        offset=0,
        max_bytes=4,
        max_file_bytes=1024,
    )
    assert first.content == "가"
    assert first.next_offset == len("가".encode())

    second = await read_text_file(
        "utf8-2",
        str(path),
        roots=roots,
        offset=first.next_offset,
        max_bytes=4,
        max_file_bytes=1024,
    )
    assert second.content == "나"


@pytest.mark.asyncio
async def test_read_rejects_binary_and_misaligned_utf8(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    roots = allowed_roots([str(root)])

    binary_path = root / "binary.dat"
    binary_path.write_bytes(b"abc\x00def")
    binary = await read_text_file(
        "binary",
        str(binary_path),
        roots=roots,
        offset=0,
        max_bytes=8,
        max_file_bytes=1024,
    )
    assert binary.rejected is True

    text_path = root / "text.txt"
    text_path.write_text("가", encoding="utf-8")
    misaligned = await read_text_file(
        "misaligned",
        str(text_path),
        roots=roots,
        offset=1,
        max_bytes=4,
        max_file_bytes=1024,
    )
    assert misaligned.rejected is True

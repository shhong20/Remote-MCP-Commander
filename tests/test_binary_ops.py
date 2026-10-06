import base64
import hashlib

import pytest

from remote_mcp_commander.agent import binary_ops
from remote_mcp_commander.agent.binary_ops import read_binary_file, write_binary_file
from remote_mcp_commander.agent.file_ops import allowed_roots


@pytest.mark.asyncio
async def test_binary_read_returns_chunk_offsets_and_whole_file_hash(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    path = root / "payload.bin"
    payload = bytes(range(32))
    path.write_bytes(payload)
    roots = allowed_roots([str(root)])

    result = await read_binary_file(
        "binary-read",
        str(path),
        roots=roots,
        offset=7,
        max_bytes=9,
        max_file_bytes=1024,
    )

    assert result.rejected is False
    assert base64.b64decode(result.data_base64) == payload[7:16]
    assert result.size == len(payload)
    assert result.offset == 7
    assert result.next_offset == 16
    assert result.eof is False
    assert result.sha256 == hashlib.sha256(payload).hexdigest()


@pytest.mark.asyncio
async def test_binary_write_create_and_hash_guarded_overwrite(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    roots = allowed_roots([str(root)])
    path = root / "payload.bin"
    first = b"\x00\x01first\xff"
    second = b"\x10\x11second\x00"

    created = await write_binary_file(
        "binary-create",
        str(path),
        base64.b64encode(first).decode("ascii"),
        roots=roots,
        overwrite=False,
        expected_sha256=None,
        max_file_bytes=1024,
    )
    assert created.rejected is False
    assert path.read_bytes() == first
    assert created.sha256 == hashlib.sha256(first).hexdigest()

    no_hash = await write_binary_file(
        "binary-no-hash",
        str(path),
        base64.b64encode(second).decode("ascii"),
        roots=roots,
        overwrite=True,
        expected_sha256=None,
        max_file_bytes=1024,
    )
    assert no_hash.rejected is True
    assert path.read_bytes() == first

    stale = await write_binary_file(
        "binary-stale",
        str(path),
        base64.b64encode(second).decode("ascii"),
        roots=roots,
        overwrite=True,
        expected_sha256="0" * 64,
        max_file_bytes=1024,
    )
    assert stale.rejected is True
    assert path.read_bytes() == first

    updated = await write_binary_file(
        "binary-update",
        str(path),
        base64.b64encode(second).decode("ascii"),
        roots=roots,
        overwrite=True,
        expected_sha256=hashlib.sha256(first).hexdigest(),
        max_file_bytes=1024,
    )
    assert updated.rejected is False
    assert path.read_bytes() == second


@pytest.mark.asyncio
async def test_binary_transfer_rejects_invalid_base64_size_and_symlink(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    roots = allowed_roots([str(root)])

    invalid = await write_binary_file(
        "invalid",
        str(root / "invalid.bin"),
        "not base64!",
        roots=roots,
        overwrite=False,
        expected_sha256=None,
        max_file_bytes=16,
    )
    assert invalid.rejected is True
    assert "invalid base64" in (invalid.error or "")

    too_large = await write_binary_file(
        "large",
        str(root / "large.bin"),
        base64.b64encode(b"x" * 17).decode("ascii"),
        roots=roots,
        overwrite=False,
        expected_sha256=None,
        max_file_bytes=16,
    )
    assert too_large.rejected is True

    target = root / "target.bin"
    target.write_bytes(b"safe")
    link = root / "link.bin"
    link.symlink_to(target)
    read_link = await read_binary_file(
        "read-link",
        str(link),
        roots=roots,
        offset=0,
        max_bytes=16,
        max_file_bytes=16,
    )
    write_link = await write_binary_file(
        "write-link",
        str(link),
        base64.b64encode(b"changed").decode("ascii"),
        roots=roots,
        overwrite=True,
        expected_sha256=hashlib.sha256(b"safe").hexdigest(),
        max_file_bytes=16,
    )
    assert read_link.rejected is True
    assert write_link.rejected is True
    assert target.read_bytes() == b"safe"


@pytest.mark.asyncio
async def test_binary_read_rejects_offset_beyond_end(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    path = root / "payload.bin"
    path.write_bytes(b"abc")
    roots = allowed_roots([str(root)])

    result = await read_binary_file(
        "offset",
        str(path),
        roots=roots,
        offset=4,
        max_bytes=2,
        max_file_bytes=16,
    )
    assert result.rejected is True
    assert "offset exceeds" in (result.error or "")


@pytest.mark.asyncio
async def test_binary_transfer_supports_empty_file(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    path = root / "empty.bin"
    roots = allowed_roots([str(root)])

    written = await write_binary_file(
        "empty-write",
        str(path),
        "",
        roots=roots,
        overwrite=False,
        expected_sha256=None,
        max_file_bytes=16,
    )
    assert written.rejected is False
    assert written.bytes_written == 0
    assert path.read_bytes() == b""

    read = await read_binary_file(
        "empty-read",
        str(path),
        roots=roots,
        offset=0,
        max_bytes=16,
        max_file_bytes=16,
    )
    assert read.rejected is False
    assert read.data_base64 == ""
    assert read.size == 0
    assert read.eof is True
    assert read.sha256 == hashlib.sha256(b"").hexdigest()


@pytest.mark.asyncio
async def test_binary_create_fails_if_target_appears_before_atomic_link(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    path = root / "race.bin"
    roots = allowed_roots([str(root)])

    def fail_link(source, destination):
        raise FileExistsError(destination)

    monkeypatch.setattr(binary_ops.os, "link", fail_link)
    result = await write_binary_file(
        "race",
        str(path),
        base64.b64encode(b"payload").decode("ascii"),
        roots=roots,
        overwrite=False,
        expected_sha256=None,
        max_file_bytes=1024,
    )

    assert result.rejected is True
    assert "target appeared during write" in (result.error or "")
    assert path.exists() is False

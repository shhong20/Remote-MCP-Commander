import hashlib
from pathlib import Path

import pytest

from remote_mcp_commander.agent.file_ops import append_text_file


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.mark.asyncio
async def test_append_text_file_appends_and_reports_hash(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("first\n")
    result = await append_text_file(
        "a1", str(path), "second\n", roots=[tmp_path.resolve()],
        expected_sha256=digest(b"first\n"), max_file_bytes=1024,
    )
    assert result.rejected is False
    assert result.bytes_appended == len(b"second\n")
    assert path.read_text() == "first\nsecond\n"
    assert result.sha256 == digest(path.read_bytes())


@pytest.mark.asyncio
async def test_append_rejects_stale_hash_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("current")
    result = await append_text_file(
        "a2", str(path), "new", roots=[tmp_path.resolve()],
        expected_sha256="0" * 64, max_file_bytes=1024,
    )
    assert result.rejected is True
    assert "changed since read" in (result.error or "")
    assert path.read_text() == "current"


@pytest.mark.asyncio
async def test_append_rejects_binary_and_size_overflow(tmp_path: Path) -> None:
    binary = tmp_path / "binary.dat"
    binary.write_bytes(b"a\x00b")
    rejected_binary = await append_text_file(
        "a3", str(binary), "x", roots=[tmp_path.resolve()],
        expected_sha256=None, max_file_bytes=1024,
    )
    assert rejected_binary.rejected is True
    path = tmp_path / "small.txt"
    path.write_text("1234")
    overflow = await append_text_file(
        "a4", str(path), "56", roots=[tmp_path.resolve()],
        expected_sha256=None, max_file_bytes=5,
    )
    assert overflow.rejected is True
    assert path.read_text() == "1234"


@pytest.mark.asyncio
async def test_append_rejects_outside_allowed_root(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    result = await append_text_file(
        "a5", str(outside), "y", roots=[allowed.resolve()],
        expected_sha256=None, max_file_bytes=1024,
    )
    assert result.rejected is True

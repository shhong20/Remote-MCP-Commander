import os
from pathlib import Path

import pytest

from remote_mcp_commander.agent.edit_ops import edit_text_file


@pytest.mark.asyncio
async def test_edit_unique_text_atomically_and_preserves_mode(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    path.chmod(0o640)
    result = await edit_text_file(
        "req",
        str(path),
        "beta",
        "BETA",
        roots=[tmp_path],
        replace_all=False,
        max_file_bytes=1024,
    )
    assert result.rejected is False
    assert result.replacements == 1
    assert path.read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o640


@pytest.mark.asyncio
async def test_edit_rejects_ambiguous_match_without_replace_all(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("x x x", encoding="utf-8")
    result = await edit_text_file(
        "req", str(path), "x", "y", roots=[tmp_path], replace_all=False, max_file_bytes=1024
    )
    assert result.rejected is True
    assert "multiple matches" in (result.error or "")
    assert path.read_text() == "x x x"


@pytest.mark.asyncio
async def test_edit_replace_all_requires_explicit_opt_in(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("x x x", encoding="utf-8")
    result = await edit_text_file(
        "req", str(path), "x", "y", roots=[tmp_path], replace_all=True, max_file_bytes=1024
    )
    assert result.rejected is False
    assert result.replacements == 3
    assert path.read_text() == "y y y"


@pytest.mark.asyncio
async def test_edit_rejects_symlink_and_binary(tmp_path: Path) -> None:
    real = tmp_path / "real.txt"
    real.write_text("hello", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    symlink_result = await edit_text_file(
        "link", str(link), "hello", "bye", roots=[tmp_path], replace_all=False, max_file_bytes=1024
    )
    assert symlink_result.rejected is True

    binary = tmp_path / "binary.bin"
    binary.write_bytes(b"hello\x00world")
    binary_result = await edit_text_file(
        "binary",
        str(binary),
        "hello",
        "bye",
        roots=[tmp_path],
        replace_all=False,
        max_file_bytes=1024,
    )
    assert binary_result.rejected is True

from __future__ import annotations

from pathlib import Path

import pytest

from remote_mcp_commander.agent.file_ops import read_many_text_files


async def run_batch(root: Path, paths: list[str], **overrides):
    return await read_many_text_files(
        "batch",
        paths,
        roots=[root.resolve()],
        max_bytes_per_file=overrides.get("max_bytes_per_file", 32_768),
        max_total_bytes=overrides.get("max_total_bytes", 262_144),
        max_file_bytes=1_048_576,
    )


@pytest.mark.asyncio
async def test_reads_multiple_text_files(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    first = root / "first.py"
    second = root / "second.py"
    first.write_text("alpha\n")
    second.write_text("beta\n")
    result = await run_batch(root, [str(first), str(second)])
    assert [item.content for item in result.files] == ["alpha\n", "beta\n"]
    assert result.requested_count == 2
    assert result.total_bytes == 11
    assert result.truncated is False


@pytest.mark.asyncio
async def test_binary_failure_does_not_abort_other_files(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    binary = root / "blob.bin"
    text = root / "ok.txt"
    binary.write_bytes(b"abc\x00def")
    text.write_text("still-readable")
    result = await run_batch(root, [str(binary), str(text)])
    assert result.files[0].rejected is True
    assert result.files[1].content == "still-readable"
    assert len(result.files) == 2


@pytest.mark.asyncio
async def test_outside_root_is_rejected_without_aborting_batch(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    inside = root / "inside.txt"
    outside.write_text("no")
    inside.write_text("yes")
    result = await run_batch(root, [str(outside), str(inside)])
    assert result.files[0].rejected is True
    assert result.files[1].content == "yes"


@pytest.mark.asyncio
async def test_total_budget_truncates_remaining_files(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    first = root / "a.txt"
    second = root / "b.txt"
    first.write_text("12345678")
    second.write_text("abcdefgh")
    result = await run_batch(
        root, [str(first), str(second)], max_bytes_per_file=8, max_total_bytes=8
    )
    assert len(result.files) == 1
    assert result.total_bytes == 8
    assert result.truncated is True


@pytest.mark.asyncio
async def test_per_file_limit_reports_partial_file(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    large = root / "large.txt"
    large.write_text("abcdefghij")
    result = await run_batch(root, [str(large)], max_bytes_per_file=4)
    assert result.files[0].content == "abcd"
    assert result.files[0].eof is False
    assert result.truncated is False

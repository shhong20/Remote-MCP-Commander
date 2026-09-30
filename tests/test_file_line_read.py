from pathlib import Path

import pytest

from remote_mcp_commander.agent.file_ops import read_text_lines


@pytest.mark.asyncio
async def test_line_read_supports_ranges_and_tail(tmp_path: Path) -> None:
    path = tmp_path / "sample.txt"
    path.write_text("one\ntwo\nthree\nfour\nfive\n")
    middle = await read_text_lines(
        "l1", str(path), roots=[tmp_path.resolve()], offset=1,
        max_lines=2, max_file_bytes=1024,
    )
    assert middle.content == "two\nthree\n"
    assert (middle.total_lines, middle.start_line, middle.next_line, middle.eof) == (5, 1, 3, False)
    tail = await read_text_lines(
        "l2", str(path), roots=[tmp_path.resolve()], offset=-2,
        max_lines=1, max_file_bytes=1024,
    )
    assert tail.content == "four\nfive\n"
    assert (tail.start_line, tail.next_line, tail.eof) == (3, 5, True)


@pytest.mark.asyncio
async def test_line_read_past_end_returns_empty_eof(tmp_path: Path) -> None:
    path = tmp_path / "sample.txt"
    path.write_text("one\ntwo\n")
    result = await read_text_lines(
        "l3", str(path), roots=[tmp_path.resolve()], offset=99,
        max_lines=20, max_file_bytes=1024,
    )
    assert result.content == ""
    assert result.start_line == result.next_line == 2
    assert result.eof is True


@pytest.mark.asyncio
async def test_line_read_rejects_binary_and_outside_root(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    binary = allowed / "binary.dat"
    binary.write_bytes(b"x\x00y")
    binary_result = await read_text_lines(
        "l4", str(binary), roots=[allowed.resolve()], offset=0,
        max_lines=20, max_file_bytes=1024,
    )
    assert binary_result.rejected is True
    outside = tmp_path / "outside.txt"
    outside.write_text("x\n")
    outside_result = await read_text_lines(
        "l5", str(outside), roots=[allowed.resolve()], offset=0,
        max_lines=20, max_file_bytes=1024,
    )
    assert outside_result.rejected is True

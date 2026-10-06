from pathlib import Path

import pytest

from remote_mcp_commander.agent.file_ops import tail_text_file


@pytest.mark.asyncio
async def test_tail_file_returns_last_lines_from_small_file(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    path.write_text("a\nb\nc\n")
    result = await tail_text_file(
        "tail", str(path), roots=[tmp_path], lines=2, max_bytes=4096
    )
    assert result.rejected is False
    assert result.content == "b\nc\n"
    assert result.lines_returned == 2
    assert result.truncated is False
    assert result.size == path.stat().st_size


@pytest.mark.asyncio
async def test_tail_file_reads_large_file_without_whole_file_ceiling(tmp_path: Path) -> None:
    path = tmp_path / "large.log"
    line = "x" * 100 + "\n"
    path.write_text(line * 20_000)
    assert path.stat().st_size > 1_048_576
    result = await tail_text_file(
        "tail", str(path), roots=[tmp_path], lines=3, max_bytes=4096
    )
    assert result.rejected is False
    assert result.content == line * 3
    assert result.lines_returned == 3
    assert result.scanned_bytes <= 4096
    assert result.truncated is False


@pytest.mark.asyncio
async def test_tail_file_marks_scan_limit_inside_long_line(tmp_path: Path) -> None:
    path = tmp_path / "long.log"
    path.write_text("header\n" + ("z" * 20_000))
    result = await tail_text_file(
        "tail", str(path), roots=[tmp_path], lines=1, max_bytes=4096
    )
    assert result.rejected is False
    assert result.content == ""
    assert result.lines_returned == 0
    assert result.scanned_bytes == 4096
    assert result.truncated is True


@pytest.mark.asyncio
async def test_tail_file_handles_utf8_boundary_after_dropping_prefix(tmp_path: Path) -> None:
    path = tmp_path / "utf8.log"
    path.write_text(("가" * 80 + "\n") * 200)
    result = await tail_text_file(
        "tail", str(path), roots=[tmp_path], lines=2, max_bytes=4096
    )
    assert result.rejected is False
    assert result.content == ("가" * 80 + "\n") * 2
    assert result.truncated is False


@pytest.mark.asyncio
async def test_tail_file_rejects_binary_tail_and_outside_root(tmp_path: Path) -> None:
    binary = tmp_path / "binary.log"
    binary.write_bytes(b"ok\n\x00bad\n")
    binary_result = await tail_text_file(
        "tail", str(binary), roots=[tmp_path], lines=2, max_bytes=4096
    )
    assert binary_result.rejected is True
    assert "binary" in (binary_result.error or "")

    outside = tmp_path.parent / "outside-tail.log"
    outside.write_text("nope\n")
    try:
        outside_result = await tail_text_file(
            "tail", str(outside), roots=[tmp_path], lines=1, max_bytes=4096
        )
        assert outside_result.rejected is True
        assert "outside" in (outside_result.error or "")
    finally:
        outside.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_tail_file_rejects_final_symlink(tmp_path: Path) -> None:
    target = tmp_path / "real.log"
    target.write_text("safe\n")
    link = tmp_path / "link.log"
    link.symlink_to(target)

    result = await tail_text_file(
        "tail", str(link), roots=[tmp_path], lines=1, max_bytes=4096
    )

    assert result.rejected is True
    assert "symlink" in (result.error or "")


@pytest.mark.asyncio
async def test_tail_file_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    import os

    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO is not supported on this platform")
    fifo = tmp_path / "events.pipe"
    os.mkfifo(fifo)

    result = await tail_text_file(
        "tail", str(fifo), roots=[tmp_path], lines=1, max_bytes=4096
    )

    assert result.rejected is True
    assert "regular file" in (result.error or "")

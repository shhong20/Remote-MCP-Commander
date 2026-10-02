from __future__ import annotations

from pathlib import Path

import pytest

from remote_mcp_commander.agent import image_ops


def png_header(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
    )


def jpeg_header(width: int, height: int) -> bytes:
    payload = b"\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x03" + b"\x00" * 9
    return b"\xff\xd8\xff\xc0" + (len(payload) + 2).to_bytes(2, "big") + payload


def gif_header(width: int, height: int) -> bytes:
    return b"GIF89a" + width.to_bytes(2, "little") + height.to_bytes(2, "little")


def webp_vp8x_header(width: int, height: int) -> bytes:
    payload = (
        b"\x00\x00\x00\x00"
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )
    return (
        b"RIFF"
        + (4 + 8 + len(payload)).to_bytes(4, "little")
        + b"WEBP"
        + b"VP8X"
        + len(payload).to_bytes(4, "little")
        + payload
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("suffix", "data", "expected_format", "mime_type", "width", "height"),
    [
        (".png", png_header(4, 3), "png", "image/png", 4, 3),
        (".jpg", jpeg_header(5, 2), "jpeg", "image/jpeg", 5, 2),
        (".gif", gif_header(6, 4), "gif", "image/gif", 6, 4),
        (".webp", webp_vp8x_header(7, 5), "webp", "image/webp", 7, 5),
    ],
)
async def test_preview_image_validates_supported_headers(
    tmp_path: Path,
    suffix: str,
    data: bytes,
    expected_format: str,
    mime_type: str,
    width: int,
    height: int,
) -> None:
    path = tmp_path / f"sample{suffix}"
    path.write_bytes(data)

    result = await image_ops.preview_image("req", str(path), roots=[tmp_path])

    assert result.rejected is False
    assert result.format == expected_format
    assert result.mime_type == mime_type
    assert result.width == width
    assert result.height == height
    assert result.size == len(data)
    assert result.data_base64
    assert result.sha256 is not None


@pytest.mark.asyncio
async def test_preview_image_rejects_extension_signature_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "fake.jpg"
    path.write_bytes(png_header(2, 2))

    result = await image_ops.preview_image("req", str(path), roots=[tmp_path])

    assert result.rejected is True
    assert "JPEG signature" in (result.error or "")


@pytest.mark.asyncio
async def test_preview_image_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "sample.png"
    target.write_bytes(png_header(2, 2))
    link = tmp_path / "linked.png"
    link.symlink_to(target)

    result = await image_ops.preview_image("req", str(link), roots=[tmp_path])

    assert result.rejected is True
    assert "symlink" in (result.error or "")


@pytest.mark.asyncio
async def test_preview_image_rejects_large_pixel_count(tmp_path: Path) -> None:
    path = tmp_path / "huge.png"
    path.write_bytes(png_header(8192, 8192))

    result = await image_ops.preview_image("req", str(path), roots=[tmp_path])

    assert result.rejected is True
    assert "pixel count" in (result.error or "")


@pytest.mark.asyncio
async def test_preview_image_rejects_file_over_one_mib(tmp_path: Path) -> None:
    path = tmp_path / "large.png"
    path.write_bytes(png_header(1, 1) + b"\x00" * image_ops.IMAGE_MAX_BYTES)

    result = await image_ops.preview_image("req", str(path), roots=[tmp_path])

    assert result.rejected is True
    assert "1 MiB" in (result.error or "")

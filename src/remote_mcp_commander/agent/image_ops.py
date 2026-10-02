from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import Path

from remote_mcp_commander.agent.file_ops import resolve_allowed_path
from remote_mcp_commander.protocol import ImagePreviewResult

IMAGE_MAX_BYTES = 1_048_576
IMAGE_MAX_WIDTH = 8192
IMAGE_MAX_HEIGHT = 8192
IMAGE_MAX_PIXELS = 33_554_432

_JPEG_SOF_MARKERS = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}


def _validate_dimensions(width: int, height: int) -> tuple[int, int]:
    if width < 1 or height < 1:
        raise ValueError("image dimensions must be positive")
    if width > IMAGE_MAX_WIDTH or height > IMAGE_MAX_HEIGHT:
        raise ValueError("image dimensions exceed preview limit")
    if width * height > IMAGE_MAX_PIXELS:
        raise ValueError("image pixel count exceeds preview limit")
    return width, height


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("invalid PNG signature")
    if data[12:16] != b"IHDR" or int.from_bytes(data[8:12], "big") != 13:
        raise ValueError("PNG IHDR is missing or invalid")
    return _validate_dimensions(
        int.from_bytes(data[16:20], "big"),
        int.from_bytes(data[20:24], "big"),
    )


def _gif_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 10 or data[:6] not in {b"GIF87a", b"GIF89a"}:
        raise ValueError("invalid GIF signature")
    return _validate_dimensions(
        int.from_bytes(data[6:8], "little"),
        int.from_bytes(data[8:10], "little"),
    )


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise ValueError("invalid JPEG signature")
    position = 2
    while position < len(data):
        if data[position] != 0xFF:
            position += 1
            continue
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            break
        marker = data[position]
        position += 1
        if marker in {0x01, *range(0xD0, 0xDA)}:
            continue
        if marker == 0xDA:
            break
        if position + 2 > len(data):
            raise ValueError("truncated JPEG segment")
        segment_length = int.from_bytes(data[position : position + 2], "big")
        if segment_length < 2 or position + segment_length > len(data):
            raise ValueError("invalid JPEG segment length")
        if marker in _JPEG_SOF_MARKERS:
            if segment_length < 7:
                raise ValueError("invalid JPEG frame header")
            height = int.from_bytes(data[position + 3 : position + 5], "big")
            width = int.from_bytes(data[position + 5 : position + 7], "big")
            return _validate_dimensions(width, height)
        position += segment_length
    raise ValueError("JPEG frame dimensions are unavailable")


def _webp_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("invalid WebP signature")
    riff_size = int.from_bytes(data[4:8], "little")
    if riff_size + 8 > len(data):
        raise ValueError("truncated WebP RIFF payload")
    chunk = data[12:16]
    chunk_size = int.from_bytes(data[16:20], "little")
    if 20 + chunk_size > len(data):
        raise ValueError("truncated WebP image chunk")

    if chunk == b"VP8X":
        if chunk_size < 10 or len(data) < 30:
            raise ValueError("invalid WebP VP8X header")
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return _validate_dimensions(width, height)

    if chunk == b"VP8L":
        if chunk_size < 5 or len(data) < 25 or data[20] != 0x2F:
            raise ValueError("invalid WebP VP8L header")
        packed = int.from_bytes(data[21:25], "little")
        width = (packed & 0x3FFF) + 1
        height = ((packed >> 14) & 0x3FFF) + 1
        return _validate_dimensions(width, height)

    if chunk == b"VP8 ":
        if chunk_size < 10 or len(data) < 30 or data[23:26] != b"\x9d\x01\x2a":
            raise ValueError("invalid WebP VP8 frame header")
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return _validate_dimensions(width, height)

    raise ValueError("unsupported WebP image chunk")


def _detect_image(data: bytes, suffix: str) -> tuple[str, str, int, int]:
    expected = {
        ".png": ("png", "image/png", _png_dimensions),
        ".jpg": ("jpeg", "image/jpeg", _jpeg_dimensions),
        ".jpeg": ("jpeg", "image/jpeg", _jpeg_dimensions),
        ".gif": ("gif", "image/gif", _gif_dimensions),
        ".webp": ("webp", "image/webp", _webp_dimensions),
    }.get(suffix.lower())
    if expected is None:
        raise ValueError("unsupported image type; expected PNG, JPEG, GIF, or WebP")
    image_format, mime_type, parser = expected
    width, height = parser(data)
    return image_format, mime_type, width, height


def _preview_image_sync(
    request_id: str,
    raw_path: str,
    *,
    roots: list[Path],
) -> ImagePreviewResult:
    try:
        requested = Path(raw_path).expanduser()
        if not requested.is_absolute():
            raise PermissionError("image path must be absolute")
        if requested.is_symlink():
            raise PermissionError("symlink images are not supported")
        path = resolve_allowed_path(raw_path, roots).resolve(strict=True)
        if not path.is_file():
            raise PermissionError("not a regular file")
        size = path.stat().st_size
        if size > IMAGE_MAX_BYTES:
            raise ValueError("image exceeds 1 MiB preview limit")
        with path.open("rb") as handle:
            data = handle.read(IMAGE_MAX_BYTES + 1)
        if len(data) > IMAGE_MAX_BYTES:
            raise ValueError("image exceeds 1 MiB preview limit")
        if len(data) != size:
            raise ValueError("image size changed while reading")
        image_format, mime_type, width, height = _detect_image(data, path.suffix)
        return ImagePreviewResult(
            request_id=request_id,
            path=str(path),
            format=image_format,
            mime_type=mime_type,
            data_base64=base64.b64encode(data).decode("ascii"),
            size=size,
            width=width,
            height=height,
            sha256=hashlib.sha256(data).hexdigest(),
        )
    except (OSError, PermissionError, ValueError) as exc:
        return ImagePreviewResult(request_id=request_id, rejected=True, error=str(exc))


async def preview_image(
    request_id: str,
    raw_path: str,
    *,
    roots: list[Path],
) -> ImagePreviewResult:
    return await asyncio.to_thread(
        _preview_image_sync,
        request_id,
        raw_path,
        roots=roots,
    )

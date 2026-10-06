import base64
import hashlib
from pathlib import Path

import pytest

from remote_mcp_commander.agent.transfer_ops import TRANSFER_CHUNK_BYTES, FileTransferManager


def payload(size: int) -> bytes:
    block = bytes(range(251))
    return (block * ((size // len(block)) + 1))[:size]


def make_manager(root: Path, *, max_bytes: int = 4_194_304) -> FileTransferManager:
    return FileTransferManager(roots=[root.resolve()], max_bytes=max_bytes, ttl_s=60, max_active=4)


@pytest.mark.asyncio
async def test_large_upload_round_trip_and_chunk_replay(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    manager = make_manager(root)
    data = payload(1_572_901)
    sha256 = hashlib.sha256(data).hexdigest()
    target = root / "large.bin"

    started = await manager.start_upload(
        "start", "1" * 32, str(target), size=len(data), sha256=sha256,
        overwrite=False, expected_sha256=None,
    )
    assert started.rejected is False
    assert started.chunk_size == TRANSFER_CHUNK_BYTES

    offset = 0
    first_encoded = ""
    while offset < len(data):
        chunk = data[offset : offset + TRANSFER_CHUNK_BYTES]
        encoded = base64.b64encode(chunk).decode("ascii")
        if offset == 0:
            first_encoded = encoded
        result = await manager.upload_chunk(
            f"chunk-{offset}", "1" * 32, offset=offset, data_base64=encoded
        )
        assert result.rejected is False
        offset = result.received

    replay = await manager.upload_chunk(
        "replay", "1" * 32, offset=0, data_base64=first_encoded
    )
    assert replay.rejected is False
    assert replay.received == len(data)
    assert replay.complete is True

    status = await manager.status("status", "1" * 32)
    assert status.kind == "upload"
    assert status.transferred == len(data)

    finished = await manager.finish_upload("finish", "1" * 32)
    assert finished.committed is True
    assert finished.sha256 == sha256
    assert target.read_bytes() == data

    missing = await manager.status("missing", "1" * 32)
    assert missing.rejected is True


@pytest.mark.asyncio
async def test_upload_rejects_offset_gap_and_replay_mismatch(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    manager = make_manager(root)
    data = b"abcdef"
    digest = hashlib.sha256(data).hexdigest()
    target = root / "small.bin"
    await manager.start_upload(
        "start", "2" * 32, str(target), size=len(data), sha256=digest,
        overwrite=False, expected_sha256=None,
    )

    gap = await manager.upload_chunk(
        "gap", "2" * 32, offset=1, data_base64=base64.b64encode(b"a").decode()
    )
    assert gap.rejected is True
    assert "expected 0" in (gap.error or "")

    accepted = await manager.upload_chunk(
        "ok", "2" * 32, offset=0, data_base64=base64.b64encode(b"abc").decode()
    )
    assert accepted.received == 3

    mismatch = await manager.upload_chunk(
        "bad-replay", "2" * 32, offset=0,
        data_base64=base64.b64encode(b"abd").decode(),
    )
    assert mismatch.rejected is True
    assert "does not match" in (mismatch.error or "")
    await manager.close("close", "2" * 32)


@pytest.mark.asyncio
async def test_upload_overwrite_fails_if_target_changes_during_session(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    manager = make_manager(root)
    target = root / "target.bin"
    target.write_bytes(b"old")
    old_sha = hashlib.sha256(b"old").hexdigest()
    new_data = b"replacement"

    started = await manager.start_upload(
        "start", "3" * 32, str(target), size=len(new_data),
        sha256=hashlib.sha256(new_data).hexdigest(), overwrite=True,
        expected_sha256=old_sha,
    )
    assert started.rejected is False
    chunk = await manager.upload_chunk(
        "chunk", "3" * 32, offset=0,
        data_base64=base64.b64encode(new_data).decode(),
    )
    assert chunk.complete is True

    target.write_bytes(b"changed externally")
    finished = await manager.finish_upload("finish", "3" * 32)
    assert finished.rejected is True
    assert "changed during upload" in (finished.error or "")
    assert target.read_bytes() == b"changed externally"
    temp = next(root.glob(".remote-mcp-upload-*"))
    assert temp.stat().st_mode & 0o777 == 0o600
    await manager.close("close", "3" * 32)


@pytest.mark.asyncio
async def test_close_discards_unfinished_upload_temp_file(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    manager = make_manager(root)
    target = root / "cancelled.bin"
    digest = hashlib.sha256(b"abc").hexdigest()
    await manager.start_upload(
        "start", "4" * 32, str(target), size=3, sha256=digest,
        overwrite=False, expected_sha256=None,
    )
    assert list(root.glob(".remote-mcp-upload-*"))

    closed = await manager.close("close", "4" * 32)
    assert closed.closed is True
    assert closed.kind == "upload"
    assert not target.exists()
    assert list(root.glob(".remote-mcp-upload-*")) == []


@pytest.mark.asyncio
async def test_large_download_round_trip_and_explicit_offsets(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    manager = make_manager(root)
    data = payload(1_400_123)
    source = root / "download.bin"
    source.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()

    started = await manager.start_download("start", "5" * 32, str(source))
    assert started.rejected is False
    assert started.size == len(data)
    assert started.sha256 == digest

    rebuilt = bytearray()
    offset = 0
    while offset < len(data):
        chunk = await manager.download_chunk(
            f"read-{offset}", "5" * 32, offset=offset, max_bytes=TRANSFER_CHUNK_BYTES
        )
        assert chunk.rejected is False
        decoded = base64.b64decode(chunk.data_base64, validate=True)
        rebuilt.extend(decoded)
        offset = chunk.next_offset
    assert bytes(rebuilt) == data

    retry = await manager.download_chunk(
        "retry", "5" * 32, offset=0, max_bytes=100
    )
    assert base64.b64decode(retry.data_base64) == data[:100]
    status = await manager.status("status", "5" * 32)
    assert status.kind == "download"
    assert status.transferred == len(data)
    assert status.sha256 == digest
    assert (await manager.close("close", "5" * 32)).closed is True


@pytest.mark.asyncio
async def test_download_fails_closed_when_source_changes(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    manager = make_manager(root)
    source = root / "mutable.bin"
    source.write_bytes(b"a" * 4096)

    started = await manager.start_download("start", "6" * 32, str(source))
    assert started.rejected is False
    source.write_bytes(b"b" * 4096)

    chunk = await manager.download_chunk(
        "chunk", "6" * 32, offset=0, max_bytes=1024
    )
    assert chunk.rejected is True
    assert "changed" in (chunk.error or "")
    await manager.close("close", "6" * 32)


@pytest.mark.asyncio
async def test_zero_byte_upload_is_supported(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    manager = make_manager(root)
    target = root / "empty.bin"
    digest = hashlib.sha256(b"").hexdigest()

    started = await manager.start_upload(
        "start", "7" * 32, str(target), size=0, sha256=digest,
        overwrite=False, expected_sha256=None,
    )
    assert started.rejected is False
    finished = await manager.finish_upload("finish", "7" * 32)
    assert finished.committed is True
    assert target.read_bytes() == b""


@pytest.mark.asyncio
async def test_expired_upload_is_reaped_and_temp_removed(tmp_path, monkeypatch) -> None:
    from remote_mcp_commander.agent import transfer_ops

    clock = [100.0]
    monkeypatch.setattr(transfer_ops.time, "monotonic", lambda: clock[0])
    root = tmp_path / "root"
    root.mkdir()
    manager = FileTransferManager(
        roots=[root.resolve()], max_bytes=1024, ttl_s=60, max_active=1
    )
    target = root / "expired.bin"
    digest = hashlib.sha256(b"abc").hexdigest()
    started = await manager.start_upload(
        "start", "8" * 32, str(target), size=3, sha256=digest,
        overwrite=False, expected_sha256=None,
    )
    assert started.rejected is False
    assert list(root.glob(".remote-mcp-upload-*"))

    clock[0] = 161.0
    status = await manager.status("status", "8" * 32)
    assert status.rejected is True
    assert "not found" in (status.error or "")
    assert list(root.glob(".remote-mcp-upload-*")) == []
    assert not target.exists()


@pytest.mark.asyncio
async def test_overwrite_upload_temp_stays_private_until_publish(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    manager = make_manager(root)
    target = root / "public.bin"
    target.write_bytes(b"old")
    target.chmod(0o644)
    old_sha = hashlib.sha256(b"old").hexdigest()
    new_data = b"new-content"
    new_sha = hashlib.sha256(new_data).hexdigest()

    started = await manager.start_upload(
        "start", "9" * 32, str(target), size=len(new_data), sha256=new_sha,
        overwrite=True, expected_sha256=old_sha,
    )
    assert started.rejected is False
    temp = next(root.glob(".remote-mcp-upload-*"))
    assert temp.stat().st_mode & 0o777 == 0o600

    await manager.upload_chunk(
        "chunk", "9" * 32, offset=0,
        data_base64=base64.b64encode(new_data).decode(),
    )
    finished = await manager.finish_upload("finish", "9" * 32)
    assert finished.committed is True
    assert target.stat().st_mode & 0o777 == 0o644
    assert target.read_bytes() == new_data

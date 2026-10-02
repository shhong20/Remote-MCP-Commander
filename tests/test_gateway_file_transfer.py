import json

import pytest
from fastapi import HTTPException

from remote_mcp_commander.config import Settings
from remote_mcp_commander.gateway import app as gateway
from remote_mcp_commander.protocol import (
    DownloadChunkBody,
    DownloadChunkResult,
    DownloadStartBody,
    DownloadStartResult,
    TransferCloseResult,
    TransferStatusResult,
    UploadChunkBody,
    UploadChunkResult,
    UploadFinishResult,
    UploadStartBody,
    UploadStartResult,
)


class FakeTransferWebSocket:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    async def send_text(self, raw: str) -> None:
        payload = json.loads(raw)
        self.payloads.append(payload)
        request_id = str(payload["request_id"])
        session_id = str(payload.get("session_id", "f" * 32))
        message_type = payload["type"]
        if message_type == "upload_start_request":
            result = UploadStartResult(
                request_id=request_id,
                session_id=session_id,
                path=str(payload["path"]),
                size=int(payload["size"]),
                sha256=str(payload["sha256"]),
            )
        elif message_type == "upload_chunk_request":
            result = UploadChunkResult(
                request_id=request_id,
                session_id=session_id,
                offset=int(payload["offset"]),
                bytes_accepted=3,
                received=int(payload["offset"]) + 3,
            )
        elif message_type == "upload_finish_request":
            result = UploadFinishResult(
                request_id=request_id,
                session_id=session_id,
                path="/home/ubuntu/large.bin",
                size=3,
                sha256="a" * 64,
                committed=True,
            )
        elif message_type == "download_start_request":
            result = DownloadStartResult(
                request_id=request_id,
                session_id=session_id,
                path=str(payload["path"]),
                size=3,
                sha256="b" * 64,
            )
        elif message_type == "download_chunk_request":
            result = DownloadChunkResult(
                request_id=request_id,
                session_id=session_id,
                data_base64="AQID",
                size=3,
                offset=int(payload["offset"]),
                next_offset=3,
                eof=True,
                sha256="b" * 64,
            )
        elif message_type == "transfer_status_request":
            result = TransferStatusResult(
                request_id=request_id,
                session_id=session_id,
                kind="download",
                path="/home/ubuntu/large.bin",
                size=3,
                transferred=3,
                sha256="b" * 64,
            )
        elif message_type == "transfer_close_request":
            result = TransferCloseResult(
                request_id=request_id,
                session_id=session_id,
                kind="download",
                closed=True,
            )
        else:
            raise AssertionError(f"unexpected message type: {message_type}")
        connection = gateway.connections["server-01"]
        connection.pending[request_id].set_result(result)


def make_settings() -> Settings:
    return Settings(
        agent_token="agent-placeholder-value",
        control_token="control-placeholder-value",
    )


def install_connection(websocket: FakeTransferWebSocket) -> None:
    connection = gateway.AgentConnection(websocket=websocket)  # type: ignore[arg-type]
    connection.capabilities = ["file.transfer_upload", "file.transfer_download"]
    gateway.connections["server-01"] = connection


@pytest.mark.asyncio
async def test_gateway_upload_never_audits_base64_payload(monkeypatch) -> None:
    websocket = FakeTransferWebSocket()
    install_connection(websocket)
    audits: list[tuple[str, dict[str, object]]] = []
    required: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        gateway, "audit", lambda event, **fields: audits.append((event, fields))
    )
    monkeypatch.setattr(
        gateway,
        "audit_required",
        lambda event, **fields: required.append((event, fields)),
    )
    try:
        started = await gateway.start_upload_transfer(
            "server-01",
            UploadStartBody(path="/home/ubuntu/large.bin", size=3, sha256="a" * 64),
            make_settings(),
        )
        chunk = await gateway.upload_transfer_chunk(
            "server-01",
            started.session_id,
            UploadChunkBody(offset=0, data_base64="AQID"),
            make_settings(),
        )
        finished = await gateway.finish_upload_transfer(
            "server-01", started.session_id, make_settings()
        )
    finally:
        gateway.connections.pop("server-01", None)

    assert chunk.bytes_accepted == 3
    assert finished.committed is True
    assert required[0][0] == "file_upload_started_requested"
    assert required[-1][0] == "file_upload_finish_requested"
    assert all("data_base64" not in fields for _, fields in audits + required)
    assert audits[1][0] == "file_upload_chunk"
    assert audits[1][1]["bytes_accepted"] == 3


@pytest.mark.asyncio
async def test_gateway_upload_finish_fails_closed_before_agent_on_required_audit_failure(
    monkeypatch,
) -> None:
    websocket = FakeTransferWebSocket()
    install_connection(websocket)

    def fail_audit(*args, **kwargs):
        raise RuntimeError("required audit unavailable")

    monkeypatch.setattr(gateway, "audit_required", fail_audit)
    try:
        with pytest.raises(RuntimeError, match="required audit unavailable"):
            await gateway.finish_upload_transfer(
                "server-01", "a" * 32, make_settings()
            )
    finally:
        gateway.connections.pop("server-01", None)
    assert websocket.payloads == []


@pytest.mark.asyncio
async def test_gateway_download_status_and_close_round_trip(monkeypatch) -> None:
    websocket = FakeTransferWebSocket()
    install_connection(websocket)
    monkeypatch.setattr(gateway, "audit", lambda *args, **kwargs: None)
    try:
        started = await gateway.start_download_transfer(
            "server-01",
            DownloadStartBody(path="/home/ubuntu/large.bin"),
            make_settings(),
        )
        chunk = await gateway.download_transfer_chunk(
            "server-01",
            started.session_id,
            DownloadChunkBody(offset=0, max_bytes=262_144),
            make_settings(),
        )
        status = await gateway.transfer_status(
            "server-01", started.session_id, make_settings()
        )
        closed = await gateway.close_transfer(
            "server-01", started.session_id, make_settings()
        )
    finally:
        gateway.connections.pop("server-01", None)

    assert chunk.data_base64 == "AQID"
    assert status.kind == "download"
    assert closed.closed is True
    assert [item["type"] for item in websocket.payloads] == [
        "download_start_request",
        "download_chunk_request",
        "transfer_status_request",
        "transfer_close_request",
    ]


@pytest.mark.asyncio
async def test_gateway_rejects_transfer_without_capability() -> None:
    websocket = FakeTransferWebSocket()
    gateway.connections["server-01"] = gateway.AgentConnection(  # type: ignore[arg-type]
        websocket=websocket
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            await gateway.start_download_transfer(
                "server-01",
                DownloadStartBody(path="/home/ubuntu/large.bin"),
                make_settings(),
            )
    finally:
        gateway.connections.pop("server-01", None)
    assert exc_info.value.status_code == 409
    assert websocket.payloads == []

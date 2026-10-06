from __future__ import annotations

import json

import httpx
import pytest

from remote_mcp_commander.config import Settings
from remote_mcp_commander.gateway import app as gateway
from remote_mcp_commander.gateway.client import GatewayClient
from remote_mcp_commander.protocol import (
    ArchiveCreateBody,
    ArchiveCreateResult,
    ArchiveExtractBody,
    ArchiveExtractResult,
)


def make_settings() -> Settings:
    return Settings(
        agent_token="agent-placeholder-value",
        control_token="control-placeholder-value",
    )


class FakeArchiveWebSocket:
    def __init__(self, result_kind: str) -> None:
        self.result_kind = result_kind
        self.payloads: list[dict[str, object]] = []

    async def send_text(self, raw: str) -> None:
        payload = json.loads(raw)
        self.payloads.append(payload)
        connection = gateway.connections["server-01"]
        if self.result_kind == "create":
            result = ArchiveCreateResult(
                request_id=payload["request_id"],
                source_path=payload["source_path"],
                output_path=payload["output_path"],
                format="zip",
                entries=2,
                total_uncompressed_bytes=10,
                archive_bytes=128,
                sha256="a" * 64,
                changed=True,
            )
        else:
            result = ArchiveExtractResult(
                request_id=payload["request_id"],
                path=payload["path"],
                destination=payload["destination"],
                format="zip",
                entries=2,
                total_uncompressed_bytes=10,
                archive_sha256="b" * 64,
                changed=True,
            )
        connection.pending[payload["request_id"]].set_result(result)


@pytest.mark.asyncio
async def test_archive_create_requires_audit_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = FakeArchiveWebSocket("create")
    connection = gateway.AgentConnection(websocket=websocket)  # type: ignore[arg-type]
    connection.capabilities = ["archive.create"]
    gateway.connections["server-01"] = connection
    monkeypatch.setattr(
        gateway,
        "audit_required",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )
    body = ArchiveCreateBody(
        source_path="/srv/private",
        output_path="/srv/bundle.zip",
        expected_tree_sha256="1" * 64,
    )
    try:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await gateway.create_agent_archive("server-01", body, make_settings())
    finally:
        gateway.connections.pop("server-01", None)
    assert websocket.payloads == []


@pytest.mark.asyncio
async def test_archive_extract_requires_audit_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = FakeArchiveWebSocket("extract")
    connection = gateway.AgentConnection(websocket=websocket)  # type: ignore[arg-type]
    connection.capabilities = ["archive.extract"]
    gateway.connections["server-01"] = connection
    monkeypatch.setattr(
        gateway,
        "audit_required",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )
    body = ArchiveExtractBody(path="/srv/private.zip", destination="/srv/restored")
    try:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await gateway.extract_agent_archive("server-01", body, make_settings())
    finally:
        gateway.connections.pop("server-01", None)
    assert websocket.payloads == []


@pytest.mark.asyncio
async def test_archive_audit_omits_source_archive_paths_and_hashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        gateway,
        "audit_required",
        lambda event, **fields: events.append((event, fields)),
    )
    monkeypatch.setattr(
        gateway,
        "audit",
        lambda event, **fields: events.append((event, fields)),
    )

    create_socket = FakeArchiveWebSocket("create")
    create_connection = gateway.AgentConnection(websocket=create_socket)  # type: ignore[arg-type]
    create_connection.capabilities = ["archive.create"]
    gateway.connections["server-01"] = create_connection
    try:
        await gateway.create_agent_archive(
            "server-01",
            ArchiveCreateBody(
                source_path="/srv/private-source",
                output_path="/srv/bundle.zip",
                expected_tree_sha256="1" * 64,
                overwrite=True,
                expected_sha256="2" * 64,
            ),
            make_settings(),
        )
    finally:
        gateway.connections.pop("server-01", None)

    extract_socket = FakeArchiveWebSocket("extract")
    extract_connection = gateway.AgentConnection(websocket=extract_socket)  # type: ignore[arg-type]
    extract_connection.capabilities = ["archive.extract"]
    gateway.connections["server-01"] = extract_connection
    try:
        await gateway.extract_agent_archive(
            "server-01",
            ArchiveExtractBody(
                path="/srv/private-archive.zip",
                destination="/srv/restored",
            ),
            make_settings(),
        )
    finally:
        gateway.connections.pop("server-01", None)

    audit_text = str(events)
    assert "/srv/private-source" not in audit_text
    assert "/srv/private-archive.zip" not in audit_text
    assert "1111111111111111" not in audit_text
    assert "2222222222222222" not in audit_text
    assert "archive_create_requested" in audit_text
    assert "archive_extract_requested" in audit_text


@pytest.mark.asyncio
async def test_archive_client_uses_structured_routes() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/archives/create"):
            return httpx.Response(
                200,
                json={
                    "request_id": "create",
                    "source_path": "/srv/source",
                    "output_path": "/srv/bundle.zip",
                    "format": "zip",
                    "entries": 1,
                    "total_uncompressed_bytes": 5,
                    "archive_bytes": 100,
                    "sha256": "a" * 64,
                    "changed": True,
                },
            )
        if request.url.path.endswith("/archives/extract"):
            return httpx.Response(
                200,
                json={
                    "request_id": "extract",
                    "path": "/srv/bundle.zip",
                    "destination": "/srv/out",
                    "format": "zip",
                    "entries": 1,
                    "total_uncompressed_bytes": 5,
                    "archive_sha256": "a" * 64,
                    "changed": True,
                },
            )
        return httpx.Response(
            200,
            json={
                "request_id": "inspect",
                "path": "/srv/bundle.zip",
                "format": "zip",
                "archive_bytes": 100,
                "entries": 1,
                "total_uncompressed_bytes": 5,
                "sha256": "a" * 64,
                "listing": [{"path": "a.txt", "size": 5, "is_dir": False}],
            },
        )

    client = GatewayClient(make_settings(), transport=httpx.MockTransport(handler))
    inspected = await client.inspect_archive("server-01", "/srv/bundle.zip")
    extracted = await client.extract_archive(
        "server-01", "/srv/bundle.zip", "/srv/out"
    )
    created = await client.create_archive(
        "server-01",
        "/srv/source",
        "/srv/bundle.zip",
        expected_tree_sha256="1" * 64,
    )

    assert inspected.entries == 1
    assert extracted.changed is True
    assert created.changed is True
    assert [request.url.path.rsplit("/", 1)[-1] for request in seen] == [
        "inspect",
        "extract",
        "create",
    ]

import json

import pytest

from remote_mcp_commander.config import Settings
from remote_mcp_commander.gateway import app as gateway
from remote_mcp_commander.protocol import PdfComposeBody, PdfComposeResult, PdfPageSource


class FakePdfWebSocket:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    async def send_text(self, raw: str) -> None:
        payload = json.loads(raw)
        self.payloads.append(payload)
        connection = gateway.connections["server-01"]
        connection.pending[payload["request_id"]].set_result(
            PdfComposeResult(
                request_id=payload["request_id"],
                output_path=payload["output_path"],
                pages_written=2,
                bytes_written=4096,
                sha256="1" * 64,
            )
        )


def make_settings() -> Settings:
    return Settings(
        agent_token="agent-placeholder-value",
        control_token="control-placeholder-value",
    )


def make_body() -> PdfComposeBody:
    return PdfComposeBody(
        output_path="/srv/result.pdf",
        sources=[PdfPageSource(path="/srv/source.pdf", start_page=1, end_page=2)],
    )


@pytest.mark.asyncio
async def test_gateway_pdf_compose_requires_audit_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = FakePdfWebSocket()
    connection = gateway.AgentConnection(websocket=websocket)  # type: ignore[arg-type]
    connection.capabilities = ["pdf.compose"]
    gateway.connections["server-01"] = connection
    monkeypatch.setattr(
        gateway,
        "audit_required",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )
    try:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await gateway.compose_agent_pdf("server-01", make_body(), make_settings())
    finally:
        gateway.connections.pop("server-01", None)
    assert websocket.payloads == []


@pytest.mark.asyncio
async def test_gateway_pdf_compose_audit_omits_source_details_and_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = FakePdfWebSocket()
    connection = gateway.AgentConnection(websocket=websocket)  # type: ignore[arg-type]
    connection.capabilities = ["pdf.compose"]
    gateway.connections["server-01"] = connection
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        gateway,
        "audit_required",
        lambda event, **fields: events.append((event, fields)),
    )
    monkeypatch.setattr(gateway, "audit", lambda event, **fields: events.append((event, fields)))
    body = PdfComposeBody(
        output_path="/srv/result.pdf",
        sources=[PdfPageSource(path="/srv/source.pdf", start_page=1, end_page=2)],
        overwrite=True,
        expected_sha256="a" * 64,
    )
    try:
        result = await gateway.compose_agent_pdf("server-01", body, make_settings())
    finally:
        gateway.connections.pop("server-01", None)

    assert result.pages_written == 2
    assert websocket.payloads[0]["type"] == "pdf_compose_request"
    audit_text = str(events)
    assert "/srv/source.pdf" not in audit_text
    assert "aaaaaaaaaaaaaaaa" not in audit_text
    assert "pdf_compose_requested" in audit_text
    assert "pdf_compose" in audit_text

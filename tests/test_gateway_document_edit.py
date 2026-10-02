import json

import pytest

from remote_mcp_commander.config import Settings
from remote_mcp_commander.gateway import app as gateway
from remote_mcp_commander.protocol import (
    DocumentEditResult,
    DocxTextReplaceBody,
    XlsxRangeEditBody,
)


class FakeDocumentEditWebSocket:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    async def send_text(self, raw: str) -> None:
        payload = json.loads(raw)
        self.payloads.append(payload)
        connection = gateway.connections["server-01"]
        connection.pending[payload["request_id"]].set_result(
            DocumentEditResult(
                request_id=payload["request_id"],
                path=payload["path"],
                kind="docx" if payload["type"] == "docx_text_replace_request" else "xlsx",
                replacements=1 if payload["type"] == "docx_text_replace_request" else 0,
                cells_updated=0 if payload["type"] == "docx_text_replace_request" else 4,
                bytes_written=1234,
                sha256="1" * 64,
            )
        )


def make_settings() -> Settings:
    return Settings(
        agent_token="agent-placeholder-value",
        control_token="control-placeholder-value",
    )


@pytest.mark.asyncio
async def test_gateway_docx_edit_requires_audit_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = FakeDocumentEditWebSocket()
    connection = gateway.AgentConnection(websocket=websocket)  # type: ignore[arg-type]
    connection.capabilities = ["document.edit"]
    gateway.connections["server-01"] = connection
    monkeypatch.setattr(
        gateway,
        "audit_required",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )
    try:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await gateway.replace_agent_docx_text(
                "server-01",
                DocxTextReplaceBody(
                    path="/srv/report.docx",
                    old_text="secret old text",
                    new_text="secret new text",
                    expected_sha256="a" * 64,
                ),
                make_settings(),
            )
    finally:
        gateway.connections.pop("server-01", None)
    assert websocket.payloads == []


@pytest.mark.asyncio
async def test_gateway_docx_edit_audit_omits_text_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = FakeDocumentEditWebSocket()
    connection = gateway.AgentConnection(websocket=websocket)  # type: ignore[arg-type]
    connection.capabilities = ["document.edit"]
    gateway.connections["server-01"] = connection
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        gateway,
        "audit_required",
        lambda event, **fields: events.append((event, fields)),
    )
    monkeypatch.setattr(gateway, "audit", lambda event, **fields: events.append((event, fields)))
    try:
        result = await gateway.replace_agent_docx_text(
            "server-01",
            DocxTextReplaceBody(
                path="/srv/report.docx",
                old_text="secret old text",
                new_text="secret new text",
                expected_sha256="a" * 64,
            ),
            make_settings(),
        )
    finally:
        gateway.connections.pop("server-01", None)

    assert result.replacements == 1
    assert websocket.payloads[0]["type"] == "docx_text_replace_request"
    audit_text = str(events)
    assert "secret old text" not in audit_text
    assert "secret new text" not in audit_text
    assert "aaaaaaaaaaaaaaaa" not in audit_text


@pytest.mark.asyncio
async def test_gateway_xlsx_edit_audit_omits_cell_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = FakeDocumentEditWebSocket()
    connection = gateway.AgentConnection(websocket=websocket)  # type: ignore[arg-type]
    connection.capabilities = ["document.edit"]
    gateway.connections["server-01"] = connection
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        gateway,
        "audit_required",
        lambda event, **fields: events.append((event, fields)),
    )
    monkeypatch.setattr(gateway, "audit", lambda event, **fields: events.append((event, fields)))
    try:
        result = await gateway.edit_agent_xlsx_range(
            "server-01",
            XlsxRangeEditBody(
                path="/srv/book.xlsx",
                sheet="Data",
                cell_range="A1:B2",
                values=[["sensitive-a", 2], [3, "sensitive-b"]],
                expected_sha256="b" * 64,
            ),
            make_settings(),
        )
    finally:
        gateway.connections.pop("server-01", None)

    assert result.cells_updated == 4
    assert websocket.payloads[0]["type"] == "xlsx_range_edit_request"
    audit_text = str(events)
    assert "sensitive-a" not in audit_text
    assert "sensitive-b" not in audit_text
    assert "bbbbbbbbbbbbbbbb" not in audit_text

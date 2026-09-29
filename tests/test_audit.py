import json
import os
import stat

import pytest

from remote_mcp_commander.gateway import audit as audit_module
from remote_mcp_commander.gateway.audit import AuditJournal, audit, audit_required, configure_audit


def test_persistent_audit_redacts_sensitive_fields(tmp_path) -> None:
    path = tmp_path / "private" / "audit.jsonl"
    configure_audit(path)
    audit(
        "demo",
        agent_id="server-01",
        approval_secret="super-secret",
        payload={"content": "document body", "safe": "ok"},
        headers={"authorization": "Bearer hidden"},
        stdout="sensitive-output",
    )

    record = json.loads(path.read_text().strip())
    assert record["agent_id"] == "server-01"
    assert record["approval_secret"] == "<redacted>"
    assert record["payload"]["content"] == "<redacted>"
    assert record["payload"]["safe"] == "ok"
    assert record["headers"]["authorization"] == "<redacted>"
    assert record["stdout"] == "<redacted>"


def test_audit_file_permissions_are_owner_only(tmp_path) -> None:
    path = tmp_path / "audit" / "events.jsonl"
    configure_audit(path)
    audit("permission_check")

    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_recent_query_is_newest_first_and_filterable(tmp_path) -> None:
    journal = AuditJournal(tmp_path / "audit.jsonl")
    for index in range(6):
        journal.append(
            {
                "event_id": str(index),
                "ts": f"2026-09-29T00:00:0{index}+00:00",
                "event": "command" if index % 2 == 0 else "ping",
                "agent_id": "server-01" if index < 5 else "server-02",
            }
        )

    records, truncated = journal.read_recent(limit=2, event="command", agent_id="server-01")
    assert [record["event_id"] for record in records] == ["4", "2"]
    assert truncated is False


def test_recent_query_reports_scan_truncation(tmp_path) -> None:
    journal = AuditJournal(tmp_path / "audit.jsonl")
    for index in range(100):
        journal.append(
            {
                "event_id": str(index),
                "ts": "2026-09-29T00:00:00+00:00",
                "event": "bulk",
                "agent_id": "server-01",
                "padding": "x" * 80,
            }
        )

    records, truncated = journal.read_recent(limit=5, max_scan_bytes=512)
    assert len(records) <= 5
    assert truncated is True


def test_audit_required_fails_closed_without_journal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit_module, "_journal", None)
    with pytest.raises(RuntimeError, match="not configured"):
        audit_required("mutation_requested", agent_id="server-01")


def test_best_effort_audit_tolerates_missing_journal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit_module, "_journal", None)
    audit("read_only_event", agent_id="server-01")


def test_existing_parent_permissions_are_not_changed(tmp_path) -> None:
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    if os.name != "nt":
        parent.chmod(0o755)
    configure_audit(parent / "audit.jsonl")

    if os.name != "nt":
        assert stat.S_IMODE(parent.stat().st_mode) == 0o755


def test_audit_rejects_symlink_target_on_posix(tmp_path) -> None:
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("O_NOFOLLOW is unavailable")
    real = tmp_path / "real.log"
    real.write_text("")
    link = tmp_path / "audit.jsonl"
    link.symlink_to(real)

    with pytest.raises(OSError):
        configure_audit(link)


@pytest.mark.asyncio
async def test_gateway_audit_query_returns_bounded_records(tmp_path) -> None:
    from remote_mcp_commander.config import Settings
    from remote_mcp_commander.gateway.app import list_audit_records

    path = tmp_path / "audit.jsonl"
    configure_audit(path)
    audit("service_status", agent_id="server-01", unit="demo.service")
    audit("agent_ping", agent_id="server-02", round_trip_ms=1.2)

    settings = Settings(audit_path=str(path), control_token="control-token-12345678")
    result = await list_audit_records(
        settings,
        limit=10,
        event="service_status",
        agent_id="server-01",
    )
    assert len(result.records) == 1
    assert result.records[0]["unit"] == "demo.service"
    assert result.scan_truncated is False


def test_query_redacts_legacy_or_manually_inserted_sensitive_fields(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text(
        json.dumps(
            {
                "event": "legacy",
                "agent_id": "server-01",
                "token": "old-plaintext-token",
                "nested": {"content": "old document body"},
            }
        )
        + "\n"
    )
    journal = AuditJournal(path)
    records, _ = journal.read_recent(limit=10)
    assert records[0]["token"] == "<redacted>"
    assert records[0]["nested"]["content"] == "<redacted>"

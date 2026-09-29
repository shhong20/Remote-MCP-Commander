import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from remote_mcp_commander.gateway import audit as audit_module
from remote_mcp_commander.gateway.audit import (
    AuditDeliveryError,
    AuditIntegrityError,
    AuditJournal,
    audit,
    audit_required,
    configure_audit,
)


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


def test_audit_records_form_verifiable_hash_chain(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    journal = configure_audit(path)
    audit("first", agent_id="server-01")
    audit("second", agent_id="server-01")

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [record["audit_sequence"] for record in records] == [1, 2]
    assert records[0]["audit_previous_hash"] == "0" * 64
    assert records[1]["audit_previous_hash"] == records[0]["audit_hash"]
    assert records[0]["audit_chain_id"] == records[1]["audit_chain_id"]

    result = journal.verify()
    assert result.valid is True
    assert result.checked_records == 2
    assert result.first_sequence == 1
    assert result.last_sequence == 2
    assert result.head_hash == records[1]["audit_hash"]


def test_audit_verification_detects_record_tampering(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    journal = configure_audit(path)
    audit("original", agent_id="server-01")
    record = json.loads(path.read_text())
    record["event"] = "tampered"
    path.write_text(json.dumps(record) + "\n")

    result = journal.verify()
    assert result.valid is False
    assert "hash mismatch" in (result.error or "")

    with pytest.raises(AuditIntegrityError, match="hash mismatch"):
        configure_audit(path)


def test_legacy_records_are_preserved_before_new_chain(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text('{"event":"legacy","event_id":"old"}\n')
    journal = configure_audit(path)
    audit("chained")

    result = journal.verify()
    assert result.valid is True
    assert result.legacy_records == 1
    assert result.checked_records == 1
    assert result.first_sequence == 1


def test_rotation_retains_bounded_files_and_queries_across_them(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    journal = AuditJournal(path, max_bytes=360, retention_files=3)
    for index in range(8):
        journal.append(
            {
                "event_id": str(index),
                "ts": "2026-09-29T00:00:00+00:00",
                "event": "rotation",
                "index": index,
            }
        )

    retained = [candidate for candidate in tmp_path.iterdir() if candidate.name.startswith("audit")]
    assert len(retained) == 3
    result = journal.verify()
    assert result.valid is True
    assert result.checked_records == 3
    assert result.first_sequence == 6
    assert result.last_sequence == 8
    assert result.anchor_hash != "0" * 64

    records, truncated = journal.read_recent(limit=10, max_scan_bytes=100_000)
    assert [record["index"] for record in records] == [7, 6, 5]
    assert truncated is False


def test_chain_state_resumes_across_restart_and_rotation(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    first = AuditJournal(path, max_bytes=360, retention_files=3)
    for index in range(5):
        first.append({"event": "restart", "event_id": str(index), "index": index})

    restarted = AuditJournal(path, max_bytes=360, retention_files=3)
    initial = restarted.initialize()
    assert initial.valid is True
    assert initial.last_sequence == 5
    restarted.append({"event": "restart", "event_id": "5", "index": 5})

    result = restarted.verify()
    assert result.valid is True
    assert result.last_sequence == 6


def test_remote_shipping_sends_chained_sanitized_record(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    configure_audit(
        tmp_path / "audit.jsonl",
        remote_url="https://audit.example.test/events",
        remote_token="remote-audit-token-value",
        remote_transport=httpx.MockTransport(handler),
    )
    audit("remote", token="must-not-leak", agent_id="server-01")

    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert requests[0].headers["authorization"] == "Bearer remote-audit-token-value"
    assert payload["token"] == "<redacted>"
    assert payload["audit_sequence"] == 1
    assert len(payload["audit_hash"]) == 64


def test_remote_shipping_failure_is_optional_or_fail_closed(tmp_path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    transport = httpx.MockTransport(handler)
    optional_path = tmp_path / "optional.jsonl"
    configure_audit(
        optional_path,
        remote_url="https://audit.example.test/events",
        remote_transport=transport,
    )
    audit_required("optional-delivery")
    assert len(optional_path.read_text().splitlines()) == 1

    required_path = tmp_path / "required.jsonl"
    configure_audit(
        required_path,
        remote_url="https://audit.example.test/events",
        remote_required=True,
        remote_transport=transport,
    )
    with pytest.raises(AuditDeliveryError):
        audit_required("required-delivery")
    assert len(required_path.read_text().splitlines()) == 1


@pytest.mark.asyncio
async def test_gateway_audit_verification_endpoint(tmp_path) -> None:
    from remote_mcp_commander.config import Settings
    from remote_mcp_commander.gateway.app import verify_audit_records

    path = tmp_path / "audit.jsonl"
    configure_audit(path)
    audit("verify-endpoint")
    settings = Settings(audit_path=str(path), control_token="control-token-12345678")

    result = await verify_audit_records(settings)
    assert result.valid is True
    assert result.checked_records == 1


def test_concurrent_appends_keep_one_contiguous_chain(tmp_path) -> None:
    journal = AuditJournal(tmp_path / "audit.jsonl")
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(
                lambda index: journal.append(
                    {
                        "event_id": str(index),
                        "ts": "2026-09-29T00:00:00+00:00",
                        "event": "concurrent",
                    }
                ),
                range(50),
            )
        )

    result = journal.verify()
    assert result.valid is True
    assert result.checked_records == 50
    assert result.first_sequence == 1
    assert result.last_sequence == 50

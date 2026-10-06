import json
import sys

import pytest

from remote_mcp_commander.audit_cli import run
from remote_mcp_commander.gateway.audit import AuditJournal


def test_audit_cli_verifies_valid_chain(tmp_path, monkeypatch, capsys) -> None:
    path = tmp_path / "audit.jsonl"
    journal = AuditJournal(path)
    journal.append({"event": "cli", "event_id": "1"})
    monkeypatch.setattr(
        sys,
        "argv",
        ["remote-mcp-audit", "verify", str(path), "--json"],
    )

    run()
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["checked_records"] == 1


def test_audit_cli_exits_nonzero_for_tampering(tmp_path, monkeypatch, capsys) -> None:
    path = tmp_path / "audit.jsonl"
    journal = AuditJournal(path)
    journal.append({"event": "cli", "event_id": "1"})
    record = json.loads(path.read_text())
    record["event"] = "tampered"
    path.write_text(json.dumps(record) + "\n")
    monkeypatch.setattr(sys, "argv", ["remote-mcp-audit", "verify", str(path)])

    with pytest.raises(SystemExit) as exc_info:
        run()
    assert exc_info.value.code == 1
    assert "invalid" in capsys.readouterr().out


def test_audit_cli_exits_nonzero_for_missing_journal(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["remote-mcp-audit", "verify", str(tmp_path / "missing.jsonl")],
    )
    with pytest.raises(SystemExit) as exc_info:
        run()
    assert exc_info.value.code == 1

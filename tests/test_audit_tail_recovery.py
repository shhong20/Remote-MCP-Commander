"""Safety regression coverage for opt-in crash-tail recovery of the audit journal."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from remote_mcp_commander.gateway import audit as audit_module
from remote_mcp_commander.gateway.audit import AuditIntegrityError, AuditJournal

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX recovery requires flock")


def _create(tmp_path: Path, count: int = 3) -> tuple[Path, bytes]:
    path = tmp_path / "audit.jsonl"
    journal = AuditJournal(path)
    for n in range(count):
        journal.append({"event": "test", "event_id": str(n)})
    return path, path.read_bytes()


def _damage(path: Path, data: bytes = b'{"event": "crash", "audit_hash": "') -> bytes:
    with path.open("ab") as handle:
        handle.write(data)
    return path.read_bytes()


def _backups(path: Path) -> list[Path]:
    return list(path.parent.glob(f"{path.name}.recovery-*.backup"))


def test_recovery_is_opt_in_and_preserves_default_fail_closed(tmp_path):
    path, _ = _create(tmp_path)
    original = _damage(path)
    with pytest.raises(AuditIntegrityError, match="invalid JSON"):
        AuditJournal(path).initialize()
    assert path.read_bytes() == original
    assert not _backups(path)


def test_recovers_only_incomplete_final_record_and_resumes_chain(tmp_path):
    path, intact = _create(tmp_path)
    damaged = _damage(path)
    journal = AuditJournal(path, recover_truncated_tail=True)
    verification = journal.initialize()
    assert verification.valid and verification.last_sequence == 3
    assert path.read_bytes() == intact
    backups = _backups(path)
    assert len(backups) == 1
    assert backups[0].read_bytes() == damaged
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    journal.append({"event": "after-restart"})
    assert AuditJournal(path).verify().last_sequence == 4
    assert json.loads(path.read_text().splitlines()[-1])["event"] == "after-restart"
    assert len(_backups(path)) == 1


def test_fsync_recovery_and_older_rotations_preserve_hash_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    original = AuditJournal(path, max_bytes=360, retention_files=3, fsync=True)
    for n in range(7):
        original.append({"event": "rotation", "event_id": str(n)})
    result = original.verify()
    assert result.valid and result.last_sequence == 7
    assert path.with_name("audit.jsonl.1").exists()
    _damage(path)
    repaired = AuditJournal(
        path,
        max_bytes=360,
        retention_files=3,
        fsync=True,
        recover_truncated_tail=True,
    )
    assert repaired.initialize().valid
    repaired.append({"event": "continued"})
    assert repaired.verify().last_sequence == 8


def test_crash_simulated_in_separate_process(tmp_path):
    path, intact = _create(tmp_path)
    script = (
        "import os,sys;"
        "fd=os.open(sys.argv[1],os.O_APPEND|os.O_WRONLY);"
        'os.write(fd,b\'{\\"event\\":\\"interrupted\');'
        "os._exit(17)"
    )
    child = subprocess.run([sys.executable, "-c", script, str(path)], check=False)
    assert child.returncode == 17
    assert path.read_bytes() != intact
    assert AuditJournal(path, recover_truncated_tail=True).initialize().valid
    assert path.read_bytes() == intact


def test_complete_bad_final_json_line_is_not_recovered(tmp_path):
    path, _ = _create(tmp_path)
    damaged = _damage(path, b'{"broken":\n')
    with pytest.raises(AuditIntegrityError, match="invalid JSON"):
        AuditJournal(path, recover_truncated_tail=True).initialize()
    assert path.read_bytes() == damaged
    assert not _backups(path)


def test_invalid_middle_line_is_not_recovered(tmp_path):
    path, _ = _create(tmp_path)
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(lines[0] + b"not-json\n" + b"".join(lines[1:]) + b'{"last":')
    original = path.read_bytes()
    with pytest.raises(AuditIntegrityError):
        AuditJournal(path, recover_truncated_tail=True).initialize()
    assert path.read_bytes() == original
    assert not _backups(path)


def test_tampered_valid_prefix_is_not_recovered(tmp_path):
    path, _ = _create(tmp_path)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records[0]["event"] = "tampered"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    original = _damage(path)
    with pytest.raises(AuditIntegrityError, match="hash mismatch"):
        AuditJournal(path, recover_truncated_tail=True).initialize()
    assert path.read_bytes() == original
    assert not _backups(path)


def test_tampered_rotated_file_is_not_recovered(tmp_path):
    path = tmp_path / "audit.jsonl"
    journal = AuditJournal(path, max_bytes=360, retention_files=3)
    for n in range(7):
        journal.append({"event": "rotated", "event_id": str(n)})
    rotated = tmp_path / "audit.jsonl.1"
    assert rotated.exists()
    records = [json.loads(line) for line in rotated.read_text().splitlines()]
    records[0]["event"] = "tampered"
    rotated.write_text("".join(json.dumps(record) + "\n" for record in records))
    damaged = _damage(path)
    with pytest.raises(AuditIntegrityError):
        AuditJournal(
            path,
            max_bytes=360,
            retention_files=3,
            recover_truncated_tail=True,
        ).initialize()
    assert path.read_bytes() == damaged
    assert not _backups(path)


def test_no_valid_prefix_refuses_recovery(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_bytes(b'{"truncated":')
    with pytest.raises(AuditIntegrityError):
        AuditJournal(path, recover_truncated_tail=True).initialize()
    assert path.read_bytes() == b'{"truncated":'
    assert not _backups(path)


def test_too_large_truncated_tail_refuses_recovery(tmp_path):
    path, _ = _create(tmp_path)
    bad = _damage(path, b"x" * 65_537)
    with pytest.raises(AuditIntegrityError):
        AuditJournal(path, recover_truncated_tail=True).initialize()
    assert path.read_bytes() == bad
    assert not _backups(path)


def test_unexpected_valid_json_without_newline_is_not_dropped(tmp_path):
    path, intact = _create(tmp_path)
    path.write_bytes(intact[:-1])
    verification = AuditJournal(path, recover_truncated_tail=True).initialize()
    assert verification.valid
    assert path.read_bytes() == intact[:-1]
    assert not _backups(path)


def test_backup_failure_keeps_original_unchanged(tmp_path, monkeypatch):
    path, _ = _create(tmp_path)
    damaged = _damage(path)
    original_open = audit_module.os.open

    def deny_backup(file, flags, *args, **kwargs):
        if ".recovery-" in str(file) and str(file).endswith(".backup"):
            raise PermissionError("backup denied")
        return original_open(file, flags, *args, **kwargs)

    monkeypatch.setattr(audit_module.os, "open", deny_backup)
    with pytest.raises(PermissionError, match="backup denied"):
        AuditJournal(path, recover_truncated_tail=True).initialize()
    assert path.read_bytes() == damaged
    assert not _backups(path)


def test_symlink_journal_remains_rejected(tmp_path):
    real = tmp_path / "real"
    real.write_bytes(b'{"interrupted":')
    link = tmp_path / "audit.jsonl"
    link.symlink_to(real)
    with pytest.raises(OSError):
        AuditJournal(link, recover_truncated_tail=True).initialize()
    assert real.read_bytes() == b'{"interrupted":'
    assert not _backups(link)

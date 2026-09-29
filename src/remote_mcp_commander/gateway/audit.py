from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]


audit_logger = logging.getLogger("remote_mcp_commander.audit")
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
    "content",
    "stdout",
    "stderr",
)
_MAX_STRING_CHARS = 512
_MAX_COLLECTION_ITEMS = 32
_MAX_DEPTH = 4


def _sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth >= _MAX_DEPTH:
        return "<truncated>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= _MAX_STRING_CHARS:
            return value
        return value[:_MAX_STRING_CHARS] + "…"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= _MAX_COLLECTION_ITEMS:
                result["<truncated>"] = True
                break
            key = str(raw_key)[:128]
            result[key] = "<redacted>" if _sensitive_key(key) else _sanitize(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        items = list(value)[:_MAX_COLLECTION_ITEMS]
        return [_sanitize(item, depth=depth + 1) for item in items]
    return _sanitize(str(value), depth=depth + 1)


class AuditJournal:
    def __init__(self, path: Path, *, fsync: bool = False) -> None:
        self.path = path
        self.fsync = fsync
        self._lock = threading.Lock()

    def _ensure_parent(self) -> None:
        parent = self.path.parent
        created = not parent.exists()
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if created and os.name != "nt":
            os.chmod(parent, 0o700)

    @staticmethod
    def _nofollow_flag() -> int:
        return int(getattr(os, "O_NOFOLLOW", 0))

    def append(self, payload: dict[str, Any]) -> None:
        self._ensure_parent()
        encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode()
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | self._nofollow_flag()
        with self._lock:
            fd = os.open(self.path, flags, 0o600)
            locked = False
            try:
                if os.name != "nt":
                    os.fchmod(fd, 0o600)
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    locked = True
                view = memoryview(encoded)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("audit append made no progress")
                    view = view[written:]
                if self.fsync:
                    os.fsync(fd)
            finally:
                if fcntl is not None and locked:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def read_recent(
        self,
        *,
        limit: int,
        event: str | None = None,
        agent_id: str | None = None,
        max_scan_bytes: int = 2_097_152,
    ) -> tuple[list[dict[str, Any]], bool]:
        try:
            fd = os.open(self.path, os.O_RDONLY | self._nofollow_flag())
        except FileNotFoundError:
            return [], False
        with os.fdopen(fd, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            start = max(0, size - max_scan_bytes)
            handle.seek(start)
            data = handle.read(max_scan_bytes)
        if start:
            separator = data.find(b"\n")
            data = b"" if separator < 0 else data[separator + 1 :]

        records: list[dict[str, Any]] = []
        for raw_line in reversed(data.splitlines()):
            try:
                record = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            if event is not None and record.get("event") != event:
                continue
            if agent_id is not None and record.get("agent_id") != agent_id:
                continue
            safe_record = {
                str(key)[:128]: "<redacted>" if _sensitive_key(str(key)) else _sanitize(value)
                for key, value in record.items()
            }
            records.append(safe_record)
            if len(records) >= limit:
                break
        return records, start > 0


_journal: AuditJournal | None = None


def configure_audit(path: Path, *, fsync: bool = False) -> AuditJournal:
    global _journal
    journal = AuditJournal(path, fsync=fsync)
    journal._ensure_parent()
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | journal._nofollow_flag()
    fd = os.open(path, flags, 0o600)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        if fsync:
            os.fsync(fd)
    finally:
        os.close(fd)
    _journal = journal
    return journal


def current_journal() -> AuditJournal | None:
    return _journal


def _payload(event: str, fields: dict[str, Any]) -> dict[str, Any]:
    safe_fields = {
        key: "<redacted>" if _sensitive_key(key) else _sanitize(value)
        for key, value in fields.items()
    }
    return {
        "event_id": uuid.uuid4().hex,
        "ts": datetime.now(UTC).isoformat(),
        "event": event[:128],
        **safe_fields,
    }


def _emit(payload: dict[str, Any], *, required: bool) -> None:
    audit_logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    journal = _journal
    if journal is None:
        if required:
            raise RuntimeError("persistent audit journal is not configured")
        return
    try:
        journal.append(payload)
    except OSError:
        audit_logger.exception("persistent audit append failed")
        if required:
            raise


def audit(event: str, **fields: Any) -> None:
    _emit(_payload(event, fields), required=False)


def audit_required(event: str, **fields: Any) -> None:
    _emit(_payload(event, fields), required=True)

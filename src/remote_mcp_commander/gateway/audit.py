from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import stat
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from remote_mcp_commander.protocol import AuditVerificationResult

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
_GENESIS_HASH = "0" * 64
_HASH_PATTERN = frozenset("0123456789abcdef")
_CHAIN_FIELDS = {
    "audit_chain_id",
    "audit_sequence",
    "audit_previous_hash",
    "audit_hash",
}


class AuditIntegrityError(RuntimeError):
    pass


class AuditDeliveryError(RuntimeError):
    pass


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
            result[key] = "<redacted>" if _sensitive_key(key) else _sanitize(
                item, depth=depth + 1
            )
        return result
    if isinstance(value, (list, tuple, set)):
        items = list(value)[:_MAX_COLLECTION_ITEMS]
        return [_sanitize(item, depth=depth + 1) for item in items]
    return _sanitize(str(value), depth=depth + 1)


def _canonical_record(record: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in record.items() if key != "audit_hash"}
    return json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _record_hash(record: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_record(record)).hexdigest()


def _valid_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HASH_PATTERN for character in value)
    )


class AuditJournal:
    def __init__(
        self,
        path: Path,
        *,
        fsync: bool = False,
        max_bytes: int = 16_777_216,
        retention_files: int = 5,
        remote_url: str = "",
        remote_token: str = "",
        remote_required: bool = False,
        remote_timeout_s: float = 2.0,
        remote_transport: httpx.BaseTransport | None = None,
    ) -> None:
        if max_bytes < 256:
            raise ValueError("audit max_bytes must be at least 256")
        if retention_files < 1:
            raise ValueError("audit retention_files must be at least 1")
        self.path = path
        self.fsync = fsync
        self.max_bytes = max_bytes
        self.retention_files = retention_files
        self.remote_url = remote_url
        self.remote_token = remote_token
        self.remote_required = remote_required
        self.remote_timeout_s = remote_timeout_s
        self.remote_transport = remote_transport
        self._lock = threading.Lock()
        self._initialized = False
        self._chain_id = ""
        self._sequence = 0
        self._last_hash = _GENESIS_HASH

    def _ensure_parent(self) -> None:
        parent = self.path.parent
        created = not parent.exists()
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if created and os.name != "nt":
            os.chmod(parent, 0o700)

    @staticmethod
    def _nofollow_flag() -> int:
        return int(getattr(os, "O_NOFOLLOW", 0))

    def _rotated_path(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")

    @staticmethod
    def _lexists(path: Path) -> bool:
        return os.path.lexists(path)

    @staticmethod
    def _require_regular_path(path: Path) -> int:
        metadata = os.lstat(path)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"audit path is not a regular file: {path.name}")
        return metadata.st_size

    def _retained_paths_oldest(self) -> list[Path]:
        paths = [
            self._rotated_path(index)
            for index in range(self.retention_files - 1, 0, -1)
            if self._lexists(self._rotated_path(index))
        ]
        if self._lexists(self.path):
            paths.append(self.path)
        return paths

    def _retained_paths_newest(self) -> list[Path]:
        paths = [self.path] if self._lexists(self.path) else []
        paths.extend(
            self._rotated_path(index)
            for index in range(1, self.retention_files)
            if self._lexists(self._rotated_path(index))
        )
        return paths

    def _open_read(self, path: Path):
        fd = os.open(path, os.O_RDONLY | self._nofollow_flag())
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(fd)
            raise OSError(f"audit path is not a regular file: {path.name}")
        return os.fdopen(fd, "rb")

    def _create_current_locked(self) -> None:
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | self._nofollow_flag()
        fd = os.open(self.path, flags, 0o600)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("audit path is not a regular file")
            if self.fsync:
                os.fsync(fd)
        finally:
            os.close(fd)

    def initialize(self) -> AuditVerificationResult:
        if self._initialized:
            return AuditVerificationResult(
                valid=True,
                chain_id=self._chain_id,
                last_sequence=self._sequence or None,
                head_hash=self._last_hash if self._sequence else None,
            )
        self._ensure_parent()
        with self._lock:
            if self._initialized:
                return self._verify_locked()
            self._create_current_locked()
            result = self._verify_locked()
            if not result.valid:
                raise AuditIntegrityError(result.error or "audit chain verification failed")
            self._chain_id = result.chain_id or uuid.uuid4().hex
            self._sequence = result.last_sequence or 0
            self._last_hash = result.head_hash or _GENESIS_HASH
            self._initialized = True
            return result

    def _verify_locked(self) -> AuditVerificationResult:
        checked = 0
        legacy = 0
        retained_files = 0
        chain_id: str | None = None
        first_sequence: int | None = None
        previous_sequence: int | None = None
        anchor_hash: str | None = None
        head_hash: str | None = None
        saw_chain = False

        try:
            for path in self._retained_paths_oldest():
                retained_files += 1
                with self._open_read(path) as handle:
                    for line_number, raw_line in enumerate(handle, start=1):
                        if not raw_line.strip():
                            continue
                        try:
                            record = json.loads(raw_line)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            return AuditVerificationResult(
                                valid=False,
                                checked_records=checked,
                                legacy_records=legacy,
                                retained_files=retained_files,
                                chain_id=chain_id,
                                first_sequence=first_sequence,
                                last_sequence=previous_sequence,
                                anchor_hash=anchor_hash,
                                head_hash=head_hash,
                                error=f"invalid JSON in {path.name}:{line_number}",
                            )
                        if not isinstance(record, dict):
                            return AuditVerificationResult(
                                valid=False,
                                checked_records=checked,
                                legacy_records=legacy,
                                retained_files=retained_files,
                                error=f"non-object record in {path.name}:{line_number}",
                            )
                        present = _CHAIN_FIELDS.intersection(record)
                        if not present:
                            if saw_chain:
                                return AuditVerificationResult(
                                    valid=False,
                                    checked_records=checked,
                                    legacy_records=legacy,
                                    retained_files=retained_files,
                                    chain_id=chain_id,
                                    first_sequence=first_sequence,
                                    last_sequence=previous_sequence,
                                    anchor_hash=anchor_hash,
                                    head_hash=head_hash,
                                    error=(
                                        "legacy record follows chain in "
                                        f"{path.name}:{line_number}"
                                    ),
                                )
                            legacy += 1
                            continue
                        if present != _CHAIN_FIELDS:
                            return AuditVerificationResult(
                                valid=False,
                                checked_records=checked,
                                legacy_records=legacy,
                                retained_files=retained_files,
                                chain_id=chain_id,
                                error=f"partial chain metadata in {path.name}:{line_number}",
                            )

                        record_chain_id = record["audit_chain_id"]
                        sequence = record["audit_sequence"]
                        previous_hash = record["audit_previous_hash"]
                        digest = record["audit_hash"]
                        if (
                            not isinstance(record_chain_id, str)
                            or len(record_chain_id) != 32
                            or not all(
                                character in _HASH_PATTERN for character in record_chain_id
                            )
                            or not _valid_hash(previous_hash)
                            or not _valid_hash(digest)
                            or not isinstance(sequence, int)
                            or isinstance(sequence, bool)
                            or sequence < 1
                        ):
                            return AuditVerificationResult(
                                valid=False,
                                checked_records=checked,
                                legacy_records=legacy,
                                retained_files=retained_files,
                                chain_id=chain_id,
                                error=f"invalid chain metadata in {path.name}:{line_number}",
                            )
                        computed = _record_hash(record)
                        if not hmac.compare_digest(digest, computed):
                            return AuditVerificationResult(
                                valid=False,
                                checked_records=checked,
                                legacy_records=legacy,
                                retained_files=retained_files,
                                chain_id=chain_id,
                                first_sequence=first_sequence,
                                last_sequence=previous_sequence,
                                anchor_hash=anchor_hash,
                                head_hash=head_hash,
                                error=f"record hash mismatch in {path.name}:{line_number}",
                            )
                        if not saw_chain:
                            saw_chain = True
                            chain_id = record_chain_id
                            first_sequence = sequence
                            anchor_hash = previous_hash
                        else:
                            if record_chain_id != chain_id:
                                return AuditVerificationResult(
                                    valid=False,
                                    checked_records=checked,
                                    legacy_records=legacy,
                                    retained_files=retained_files,
                                    chain_id=chain_id,
                                    error=f"chain ID changed in {path.name}:{line_number}",
                                )
                            if sequence != (previous_sequence or 0) + 1:
                                return AuditVerificationResult(
                                    valid=False,
                                    checked_records=checked,
                                    legacy_records=legacy,
                                    retained_files=retained_files,
                                    chain_id=chain_id,
                                    error=f"sequence gap in {path.name}:{line_number}",
                                )
                            if not hmac.compare_digest(previous_hash, head_hash or ""):
                                return AuditVerificationResult(
                                    valid=False,
                                    checked_records=checked,
                                    legacy_records=legacy,
                                    retained_files=retained_files,
                                    chain_id=chain_id,
                                    error=f"previous hash mismatch in {path.name}:{line_number}",
                                )
                        previous_sequence = sequence
                        head_hash = digest
                        checked += 1
        except OSError as exc:
            return AuditVerificationResult(
                valid=False,
                checked_records=checked,
                legacy_records=legacy,
                retained_files=retained_files,
                chain_id=chain_id,
                first_sequence=first_sequence,
                last_sequence=previous_sequence,
                anchor_hash=anchor_hash,
                head_hash=head_hash,
                error=f"audit file read failed: {type(exc).__name__}",
            )

        return AuditVerificationResult(
            valid=True,
            checked_records=checked,
            legacy_records=legacy,
            retained_files=retained_files,
            chain_id=chain_id,
            first_sequence=first_sequence,
            last_sequence=previous_sequence,
            anchor_hash=anchor_hash,
            head_hash=head_hash,
        )

    def verify(self) -> AuditVerificationResult:
        self._ensure_parent()
        with self._lock:
            if not self._retained_paths_oldest():
                return AuditVerificationResult(valid=False, error="audit journal not found")
            result = self._verify_locked()
            if (
                result.valid
                and self._initialized
                and self._sequence
                and (
                    result.last_sequence != self._sequence
                    or result.head_hash != self._last_hash
                )
            ):
                return result.model_copy(
                    update={
                        "valid": False,
                        "error": "retained audit head changed after initialization",
                    }
                )
            return result

    def _rotate_locked(self) -> None:
        backup_count = self.retention_files - 1
        if backup_count:
            oldest = self._rotated_path(backup_count)
            if self._lexists(oldest):
                self._require_regular_path(oldest)
                oldest.unlink()
            for index in range(backup_count - 1, 0, -1):
                source = self._rotated_path(index)
                if not self._lexists(source):
                    continue
                self._require_regular_path(source)
                os.replace(source, self._rotated_path(index + 1))
            if self._lexists(self.path) and self._require_regular_path(self.path) > 0:
                os.replace(self.path, self._rotated_path(1))
        elif self._lexists(self.path):
            self._require_regular_path(self.path)
            self.path.unlink()
        self._create_current_locked()
        if self.fsync and os.name != "nt":
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

    def _write_locked(self, encoded: bytes) -> None:
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | self._nofollow_flag()
        fd = os.open(self.path, flags, 0o600)
        locked = False
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("audit path is not a regular file")
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

    def _deliver_remote(self, record: dict[str, Any]) -> None:
        if not self.remote_url:
            return
        headers = {"Content-Type": "application/json"}
        if self.remote_token:
            headers["Authorization"] = f"Bearer {self.remote_token}"
        try:
            with httpx.Client(
                timeout=self.remote_timeout_s,
                trust_env=False,
                follow_redirects=False,
                transport=self.remote_transport,
            ) as client:
                with client.stream(
                    "POST", self.remote_url, headers=headers, json=record
                ) as response:
                    response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AuditDeliveryError(
                f"remote audit delivery failed: {type(exc).__name__}"
            ) from exc

    def append(self, payload: dict[str, Any]) -> None:
        self.initialize()
        with self._lock:
            record = {
                **payload,
                "audit_chain_id": self._chain_id,
                "audit_sequence": self._sequence + 1,
                "audit_previous_hash": self._last_hash,
            }
            record["audit_hash"] = _record_hash(record)
            encoded = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode()
            current_size = (
                self._require_regular_path(self.path) if self._lexists(self.path) else 0
            )
            if current_size > 0 and current_size + len(encoded) > self.max_bytes:
                self._rotate_locked()
            try:
                self._write_locked(encoded)
            except OSError:
                self._initialized = False
                raise
            self._sequence = record["audit_sequence"]
            self._last_hash = record["audit_hash"]
            try:
                self._deliver_remote(record)
            except AuditDeliveryError:
                audit_logger.exception("remote audit delivery failed")
                if self.remote_required:
                    raise

    def read_recent(
        self,
        *,
        limit: int,
        event: str | None = None,
        agent_id: str | None = None,
        max_scan_bytes: int = 2_097_152,
    ) -> tuple[list[dict[str, Any]], bool]:
        self.initialize()
        records: list[dict[str, Any]] = []
        remaining = max_scan_bytes
        scan_truncated = False
        with self._lock:
            paths = self._retained_paths_newest()
            for path_index, path in enumerate(paths):
                if remaining <= 0:
                    scan_truncated = True
                    break
                with self._open_read(path) as handle:
                    size = os.fstat(handle.fileno()).st_size
                    take = min(size, remaining)
                    start = size - take
                    handle.seek(start)
                    data = handle.read(take)
                remaining -= take
                if start:
                    scan_truncated = True
                    separator = data.find(b"\n")
                    data = b"" if separator < 0 else data[separator + 1 :]

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
                        str(key)[:128]: (
                            "<redacted>" if _sensitive_key(str(key)) else _sanitize(value)
                        )
                        for key, value in record.items()
                    }
                    records.append(safe_record)
                    if len(records) >= limit:
                        return records, scan_truncated
                if remaining <= 0 and path_index + 1 < len(paths):
                    scan_truncated = True
        return records, scan_truncated


_journal: AuditJournal | None = None


def configure_audit(
    path: Path,
    *,
    fsync: bool = False,
    max_bytes: int = 16_777_216,
    retention_files: int = 5,
    remote_url: str = "",
    remote_token: str = "",
    remote_required: bool = False,
    remote_timeout_s: float = 2.0,
    remote_transport: httpx.BaseTransport | None = None,
) -> AuditJournal:
    global _journal
    journal = AuditJournal(
        path,
        fsync=fsync,
        max_bytes=max_bytes,
        retention_files=retention_files,
        remote_url=remote_url,
        remote_token=remote_token,
        remote_required=remote_required,
        remote_timeout_s=remote_timeout_s,
        remote_transport=remote_transport,
    )
    journal.initialize()
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
    except (OSError, AuditDeliveryError, AuditIntegrityError):
        audit_logger.exception("persistent audit append failed")
        if required:
            raise


def audit(event: str, **fields: Any) -> None:
    _emit(_payload(event, fields), required=False)


def audit_required(event: str, **fields: Any) -> None:
    _emit(_payload(event, fields), required=True)

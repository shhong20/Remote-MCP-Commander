from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel


class StoredDevice(BaseModel):
    agent_id: str
    token_sha256: str
    created_at: datetime
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class EnrollmentGrant:
    agent_id: str
    code_sha256: str
    expires_at: datetime


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class DeviceRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._grants: dict[str, EnrollmentGrant] = {}

    def _load_devices(self) -> dict[str, StoredDevice]:
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        devices = raw.get("devices", {})
        if not isinstance(devices, dict):
            raise ValueError("registry devices must be an object")
        return {
            str(agent_id): StoredDevice.model_validate(record)
            for agent_id, record in devices.items()
        }

    def _write_devices(self, devices: dict[str, StoredDevice]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "devices": {
                agent_id: record.model_dump(mode="json")
                for agent_id, record in sorted(devices.items())
            }
        }
        serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
        os.replace(temp_path, self.path)
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    def _prune_expired_grants(self) -> None:
        now = datetime.now(UTC)
        expired = [key for key, grant in self._grants.items() if grant.expires_at <= now]
        for key in expired:
            self._grants.pop(key, None)

    async def create_enrollment(self, agent_id: str, ttl_s: int) -> tuple[str, datetime]:
        code = secrets.token_urlsafe(16)
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_s)
        grant = EnrollmentGrant(
            agent_id=agent_id,
            code_sha256=sha256_text(code),
            expires_at=expires_at,
        )
        async with self._lock:
            self._grants[grant.code_sha256] = grant
            self._prune_expired_grants()
        return code, expires_at

    async def claim_enrollment(self, agent_id: str, code: str) -> str:
        code_hash = sha256_text(code)
        async with self._lock:
            self._prune_expired_grants()
            grant = self._grants.get(code_hash)
            if grant is None or grant.agent_id != agent_id:
                raise ValueError("invalid or expired enrollment code")
            self._grants.pop(code_hash, None)

            token = secrets.token_urlsafe(32)
            devices = self._load_devices()
            devices[agent_id] = StoredDevice(
                agent_id=agent_id,
                token_sha256=sha256_text(token),
                created_at=datetime.now(UTC),
            )
            self._write_devices(devices)
            return token

    async def get(self, agent_id: str) -> StoredDevice | None:
        async with self._lock:
            return self._load_devices().get(agent_id)

    async def verify(self, agent_id: str, token: str) -> bool:
        record = await self.get(agent_id)
        if record is None or record.revoked_at is not None:
            return False
        return secrets.compare_digest(record.token_sha256, sha256_text(token))

    async def revoke(self, agent_id: str) -> bool:
        async with self._lock:
            devices = self._load_devices()
            record = devices.get(agent_id)
            if record is None:
                return False
            if record.revoked_at is None:
                record.revoked_at = datetime.now(UTC)
                devices[agent_id] = record
                self._write_devices(devices)
            return True

    async def list_devices(self) -> list[StoredDevice]:
        async with self._lock:
            devices = self._load_devices()
            return [devices[key] for key in sorted(devices)]

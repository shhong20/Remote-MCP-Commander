from __future__ import annotations

import asyncio
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class ApprovalGrant:
    approval_id: str
    secret_hash: str
    agent_id: str
    operation: str
    target: str
    created_at: datetime
    expires_at: datetime


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class ApprovalError(ValueError):
    pass


class ApprovalStore:
    def __init__(self) -> None:
        self._grants: dict[str, ApprovalGrant] = {}
        self._lock = asyncio.Lock()

    async def issue(
        self,
        *,
        agent_id: str,
        operation: str,
        target: str,
        ttl_s: int,
    ) -> tuple[ApprovalGrant, str]:
        now = datetime.now(UTC)
        approval_id = secrets.token_urlsafe(12)
        secret = secrets.token_urlsafe(32)
        grant = ApprovalGrant(
            approval_id=approval_id,
            secret_hash=_hash_secret(secret),
            agent_id=agent_id,
            operation=operation,
            target=target,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_s),
        )
        async with self._lock:
            self._purge_expired(now)
            self._grants[approval_id] = grant
        return grant, secret

    async def consume(
        self,
        *,
        approval_id: str,
        secret: str,
        agent_id: str,
        operation: str,
        target: str,
    ) -> ApprovalGrant:
        now = datetime.now(UTC)
        async with self._lock:
            self._purge_expired(now)
            grant = self._grants.get(approval_id)
            if grant is None:
                raise ApprovalError("approval not found or already consumed")
            if not secrets.compare_digest(grant.secret_hash, _hash_secret(secret)):
                raise ApprovalError("approval secret mismatch")
            if grant.agent_id != agent_id:
                raise ApprovalError("approval agent mismatch")
            if grant.operation != operation:
                raise ApprovalError("approval operation mismatch")
            if grant.target != target:
                raise ApprovalError("approval target mismatch")
            self._grants.pop(approval_id, None)
            return grant

    def _purge_expired(self, now: datetime) -> None:
        expired = [key for key, grant in self._grants.items() if grant.expires_at <= now]
        for key in expired:
            self._grants.pop(key, None)

import asyncio

import pytest

from remote_mcp_commander.gateway.approvals import ApprovalError, ApprovalStore


@pytest.mark.asyncio
async def test_approval_is_hash_only_and_single_use() -> None:
    store = ApprovalStore()
    grant, secret = await store.issue(
        agent_id="server-01",
        operation="service.restart",
        target="demo.service",
        ttl_s=60,
    )
    assert grant.secret_hash != secret
    assert len(grant.secret_hash) == 64

    consumed = await store.consume(
        approval_id=grant.approval_id,
        secret=secret,
        agent_id="server-01",
        operation="service.restart",
        target="demo.service",
    )
    assert consumed.approval_id == grant.approval_id

    with pytest.raises(ApprovalError, match="consumed"):
        await store.consume(
            approval_id=grant.approval_id,
            secret=secret,
            agent_id="server-01",
            operation="service.restart",
            target="demo.service",
        )


@pytest.mark.asyncio
async def test_wrong_binding_does_not_burn_approval() -> None:
    store = ApprovalStore()
    grant, secret = await store.issue(
        agent_id="server-01",
        operation="service.stop",
        target="demo.service",
        ttl_s=60,
    )
    with pytest.raises(ApprovalError, match="target mismatch"):
        await store.consume(
            approval_id=grant.approval_id,
            secret=secret,
            agent_id="server-01",
            operation="service.stop",
            target="other.service",
        )

    consumed = await store.consume(
        approval_id=grant.approval_id,
        secret=secret,
        agent_id="server-01",
        operation="service.stop",
        target="demo.service",
    )
    assert consumed.target == "demo.service"


@pytest.mark.asyncio
async def test_concurrent_consumers_only_allow_one() -> None:
    store = ApprovalStore()
    grant, secret = await store.issue(
        agent_id="server-01",
        operation="process.terminate",
        target="pid:123@456",
        ttl_s=60,
    )

    async def consume() -> bool:
        try:
            await store.consume(
                approval_id=grant.approval_id,
                secret=secret,
                agent_id="server-01",
                operation="process.terminate",
                target="pid:123@456",
            )
            return True
        except ApprovalError:
            return False

    outcomes = await asyncio.gather(consume(), consume())
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 1

import os
import stat

import pytest

from remote_mcp_commander.gateway.registry import DeviceRegistry


@pytest.mark.asyncio
async def test_enrollment_claim_is_one_time_and_hashes_token(tmp_path) -> None:
    path = tmp_path / "registry.json"
    registry = DeviceRegistry(path)
    code, _ = await registry.create_enrollment("server-01", 300)

    token = await registry.claim_enrollment("server-01", code)
    assert await registry.verify("server-01", token) is True

    contents = path.read_text(encoding="utf-8")
    assert token not in contents
    assert code not in contents
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    with pytest.raises(ValueError, match="invalid or expired"):
        await registry.claim_enrollment("server-01", code)


@pytest.mark.asyncio
async def test_registry_persists_and_revocation_blocks_token(tmp_path) -> None:
    path = tmp_path / "registry.json"
    first = DeviceRegistry(path)
    code, _ = await first.create_enrollment("server-02", 300)
    token = await first.claim_enrollment("server-02", code)

    second = DeviceRegistry(path)
    assert await second.verify("server-02", token) is True
    assert await second.revoke("server-02") is True
    assert await second.verify("server-02", token) is False

    record = await second.get("server-02")
    assert record is not None
    assert record.revoked_at is not None


import pytest

from remote_mcp_commander.config import Settings
from remote_mcp_commander.gateway import app as gateway


def make_settings(tmp_path) -> Settings:
    return Settings(
        agent_token="static-agent-credential-value",
        control_token="control-credential-value",
        registry_path=str(tmp_path / "registry.json"),
    )


@pytest.mark.asyncio
async def test_registry_identity_overrides_static_fallback(tmp_path) -> None:
    gateway.registry_for.cache_clear()
    settings = make_settings(tmp_path)
    registry = gateway.registry_for(str(settings.registry_file))
    code, _ = await registry.create_enrollment("server-01", 300)
    token = await registry.claim_enrollment("server-01", code)

    source = await gateway.agent_auth_source(
        "server-01",
        f"Bearer {token}",
        settings,
    )
    assert source == "registry"

    static_source = await gateway.agent_auth_source(
        "server-01",
        "Bearer static-agent-credential-value",
        settings,
    )
    assert static_source is None

    assert await registry.revoke("server-01") is True
    revoked_source = await gateway.agent_auth_source(
        "server-01",
        f"Bearer {token}",
        settings,
    )
    assert revoked_source is None


@pytest.mark.asyncio
async def test_static_fallback_only_when_no_registry_identity_exists(tmp_path) -> None:
    gateway.registry_for.cache_clear()
    settings = make_settings(tmp_path)
    source = await gateway.agent_auth_source(
        "server-legacy",
        "Bearer static-agent-credential-value",
        settings,
    )
    assert source == "static"

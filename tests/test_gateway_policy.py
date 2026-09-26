import pytest
from fastapi import HTTPException

from remote_mcp_commander.config import Settings
from remote_mcp_commander.gateway.app import enforce_agent_policy


def make_settings(**overrides: str) -> Settings:
    values = {
        "agent_token": "default-agent-token-1234",
        "control_token": "control-token-12345678",
    }
    values.update(overrides)
    return Settings(**values)


def test_agent_specific_token_overrides_default() -> None:
    settings = make_settings(agent_tokens_json='{"server-01":"specific-token-123456"}')
    assert settings.token_for_agent("server-01") == "specific-token-123456"
    assert settings.token_for_agent("server-02") == "default-agent-token-1234"


def test_gateway_policy_allows_listed_executable() -> None:
    settings = make_settings(agent_policies_json='{"server-01":["hostname","uptime"]}')
    enforce_agent_policy("server-01", ["/bin/hostname"], settings)


def test_gateway_policy_denies_unlisted_executable() -> None:
    settings = make_settings(agent_policies_json='{"server-01":["hostname"]}')
    with pytest.raises(HTTPException) as exc_info:
        enforce_agent_policy("server-01", ["whoami"], settings)
    assert exc_info.value.status_code == 403


def test_missing_gateway_policy_remains_agent_side_only() -> None:
    settings = make_settings(agent_policies_json="{}")
    enforce_agent_policy("server-99", ["whoami"], settings)


def test_gateway_startup_rejects_example_credentials() -> None:
    settings = make_settings(agent_token="change-me-agent-token")
    with pytest.raises(ValueError, match="static Agent credentials"):
        settings.validate_gateway_security()


def test_agent_startup_rejects_example_credential() -> None:
    settings = make_settings(agent_token="change-me-agent-token")
    with pytest.raises(ValueError, match="enroll the Agent"):
        settings.validate_agent_security(settings.agent_token)

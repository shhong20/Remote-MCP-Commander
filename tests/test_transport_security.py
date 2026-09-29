import pytest

from remote_mcp_commander.config import Settings


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "agent_token": "agent-credential-value",
        "control_token": "control-credential-value",
    }
    values.update(overrides)
    return Settings(**values)


def test_agent_allows_loopback_ws_and_remote_wss() -> None:
    loopback = make_settings(gateway_ws="ws://127.0.0.1:8765/ws/agent/server-01")
    loopback.validate_agent_security(loopback.agent_token)

    remote = make_settings(gateway_ws="wss://gateway.example.test/ws/agent/server-01")
    remote.validate_agent_security(remote.agent_token)


def test_agent_rejects_remote_plaintext_websocket() -> None:
    settings = make_settings(gateway_ws="ws://gateway.example.test/ws/agent/server-01")
    with pytest.raises(ValueError, match="wss"):
        settings.validate_agent_security(settings.agent_token)


def test_mcp_gateway_allows_loopback_http_and_remote_https() -> None:
    make_settings(gateway_http="http://127.0.0.1:8765").validate_mcp_gateway_security()
    make_settings(gateway_http="https://gateway.example.test").validate_mcp_gateway_security()


def test_mcp_gateway_rejects_remote_plaintext_http() -> None:
    settings = make_settings(gateway_http="http://gateway.example.test")
    with pytest.raises(ValueError, match="https"):
        settings.validate_mcp_gateway_security()


def test_remote_mcp_http_requires_https_metadata_urls() -> None:
    settings = make_settings(
        mcp_transport="streamable-http",
        mcp_host="0.0.0.0",
        mcp_token="mcp-credential-value",
        mcp_resource_url="http://gateway.example.test/mcp",
        mcp_issuer_url="http://gateway.example.test",
    )
    with pytest.raises(ValueError, match="https"):
        settings.validate_mcp_http_security()


def test_agent_rejects_session_history_smaller_than_active_limit() -> None:
    settings = make_settings(session_max_active=20, session_history_limit=10)
    with pytest.raises(ValueError, match="history limit"):
        settings.validate_agent_security(settings.agent_token)


def test_agent_rejects_gateway_path_for_different_agent_id() -> None:
    settings = make_settings(
        agent_id="server-02",
        gateway_ws="wss://gateway.example.test/ws/agent/server-01",
    )
    with pytest.raises(ValueError, match="does not match configured Agent ID"):
        settings.validate_agent_security(settings.agent_token)


def test_remote_url_validation_error_does_not_echo_url_userinfo_secret() -> None:
    secret = "URL_USERINFO_SECRET_12345"
    settings = make_settings(
        gateway_ws=f"ws://user:{secret}@gateway.example.test/ws/agent/server-01"
    )

    with pytest.raises(ValueError) as exc_info:
        settings.validate_agent_security(settings.agent_token)

    assert secret not in str(exc_info.value)

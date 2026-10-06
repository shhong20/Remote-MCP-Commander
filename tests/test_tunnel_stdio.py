import os

import pytest

from remote_mcp_commander.tunnel_stdio import run


def test_tunnel_child_forces_stdio_and_excludes_parent_secrets(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "COMMANDER_APPROVAL_ADMIN_TOKEN=dotenv-secret\nCOMMANDER_MCP_TRANSPORT=streamable-http\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("COMMANDER_CONTROL_TOKEN", "c" * 32)
    monkeypatch.setenv("COMMANDER_GATEWAY_HTTP", "http://127.0.0.1:8765")
    monkeypatch.setenv("COMMANDER_MCP_TRANSPORT", "streamable-http")
    secrets = (
        "CONTROL_PLANE_API_KEY",
        "OPENAI_API_KEY",
        "COMMANDER_APPROVAL_ADMIN_TOKEN",
        "COMMANDER_AGENT_TOKEN",
        "COMMANDER_AGENT_TOKENS_JSON",
        "COMMANDER_MCP_TOKEN",
        "COMMANDER_AUDIT_REMOTE_TOKEN",
    )
    for name in secrets:
        monkeypatch.setenv(name, "sensitive-value")
    observed = []

    class Server:
        def run(self, *, transport):
            assert transport == "stdio"
            assert all(name not in os.environ for name in secrets)

    def build(settings):
        observed.append(settings)
        assert settings.mcp_transport == "stdio"
        assert settings.control_token == "c" * 32
        assert settings.approval_admin_token == ""
        assert settings.agent_token == ""
        assert settings.agent_tokens == {}
        assert settings.mcp_token == ""
        return Server()

    monkeypatch.setattr("remote_mcp_commander.tunnel_stdio.build_mcp", build)
    run()
    assert len(observed) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "url",
    [
        "http://public.example.test",
        "https://127.0.0.1:8765",
        "http://user:secret@127.0.0.1:8765",
        "http://127.0.0.1:8765?secret=x",
    ],
)
def test_tunnel_child_rejects_nonlocal_or_credential_bearing_gateway(monkeypatch, url):
    monkeypatch.setenv("COMMANDER_GATEWAY_HTTP", url)
    with pytest.raises(ValueError, match="loopback") as error:
        run()
    assert "secret" not in str(error.value)

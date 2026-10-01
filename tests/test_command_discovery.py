from remote_mcp_commander.agent import capabilities
from remote_mcp_commander.config import Settings


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "agent_token": "agent-placeholder-value",
        "control_token": "control-placeholder-value",
        "operation_mode": "personal",
    }
    values.update(overrides)
    return Settings(**values)


def test_command_discovery_returns_names_without_resolved_paths(monkeypatch) -> None:
    monkeypatch.setattr(
        capabilities,
        "resolve_generic_executable",
        lambda name, search_path=None: f"/usr/bin/{name}" if name in {"bash", "git"} else None,
    )
    monkeypatch.setattr(
        capabilities,
        "resolve_pty_executable",
        lambda name, search_path=None: f"/usr/bin/{name}" if name == "bash" else None,
    )
    result = capabilities.discover_commands(make_settings(), "req")
    assert result.operation_mode == "personal"
    assert {"bash", "git"}.issubset(result.generic_available)
    assert "rg" in result.generic_unavailable
    assert result.pty_available == ["bash"]
    assert all("/" not in name for name in result.generic_available + result.pty_available)

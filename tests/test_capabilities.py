from pathlib import Path

from remote_mcp_commander.agent import capabilities
from remote_mcp_commander.config import Settings
from remote_mcp_commander.protocol import AgentHello


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "agent_token": "agent-placeholder-value",
        "control_token": "control-placeholder-value",
        "allowed_executables": "echo,hostname,whoami,uptime",
    }
    values.update(overrides)
    return Settings(**values)


def test_capabilities_are_minimal_without_optional_tools_or_roots(monkeypatch) -> None:
    monkeypatch.setattr(capabilities.shutil, "which", lambda *args, **kwargs: None)
    monkeypatch.setattr(capabilities, "resolve_generic_executable", lambda *args, **kwargs: None)

    result = capabilities.detect_capabilities(make_settings(), [])

    assert result == sorted(capabilities.BASE_CAPABILITIES)
    assert "file.read" not in result
    assert "git.status" not in result
    assert "command.execute" not in result


def test_capabilities_reflect_available_tools_and_allowed_roots(
    monkeypatch, tmp_path: Path
) -> None:
    root = tmp_path.resolve()

    monkeypatch.setattr(
        capabilities.shutil,
        "which",
        lambda name, path=None: (
            f"/usr/bin/{name}" if name in {"systemctl", "journalctl", "git"} else None
        ),
    )
    monkeypatch.setattr(
        capabilities,
        "resolve_generic_executable",
        lambda name, search_path=None: f"/usr/bin/{name}" if name == "uptime" else None,
    )

    result = capabilities.detect_capabilities(make_settings(), [root])

    expected = {
        *capabilities.BASE_CAPABILITIES,
        "service.status",
        "service.action",
        "service.logs",
        "filesystem.discovery",
        "file.read",
        "file.write",
        "git.status",
        "command.execute",
        "command.session",
    }
    assert result == sorted(expected)


def test_git_capability_requires_allowed_root(monkeypatch) -> None:
    monkeypatch.setattr(
        capabilities.shutil,
        "which",
        lambda name, path=None: "/usr/bin/git" if name == "git" else None,
    )
    monkeypatch.setattr(capabilities, "resolve_generic_executable", lambda *args, **kwargs: None)

    result = capabilities.detect_capabilities(make_settings(), [])

    assert "git.status" not in result
    assert "filesystem.discovery" not in result


def test_agent_hello_remains_backward_compatible_without_capabilities() -> None:
    hello = AgentHello.model_validate(
        {
            "type": "hello",
            "agent_id": "server-01",
            "hostname": "worker-01",
            "platform": "Linux",
            "version": "0.12.0",
        }
    )

    assert hello.capabilities == []


def test_agent_hello_rejects_invalid_capability_names() -> None:
    from pydantic import ValidationError

    try:
        AgentHello(
            agent_id="server-01",
            hostname="worker-01",
            platform="Linux",
            capabilities=["INVALID CAPABILITY"],
        )
    except ValidationError:
        pass
    else:
        raise AssertionError("invalid capability name was accepted")


def test_gateway_agent_info_exposes_advertised_capabilities() -> None:
    from remote_mcp_commander.gateway.app import AgentConnection, agent_info

    connection = AgentConnection(websocket=None)  # type: ignore[arg-type]
    connection.capabilities = ["git.status", "file.read"]

    info = agent_info("server-01", connection)

    assert info.capabilities == ["git.status", "file.read"]

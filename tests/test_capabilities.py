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
        "filesystem.tree_list",
        "filesystem.search",
        "filesystem.search_session",
        "filesystem.tree",
        "file.read",
        "file.read_lines",
        "file.tail",
        "file.read_many",
        "file.binary_read",
        "file.binary_write",
        "file.transfer_upload",
        "file.transfer_download",
        "document.preview",
        "document.edit",
        "pdf.compose",
        "image.preview",
        "file.write",
        "file.append",
        "file.edit",
        "command.cwd",
        "git.status",
        "command.execute",
        "command.session",
        "command.stdin",
        "command.timeout",
        "command.output_lines",
        "command.output_wait",
        "command.session_list",
    }
    assert result == sorted(expected)


def test_personal_mode_advertises_filesystem_mutation(tmp_path: Path) -> None:
    result = capabilities.detect_capabilities(
        make_settings(operation_mode="personal"), [tmp_path.resolve()]
    )
    assert "filesystem.mutate" in result
    assert "filesystem.tree_mutate" in result
    assert "command.env" in result
    assert "command.session_signal" in result


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


def test_pty_capability_requires_dedicated_allowlist_and_posix(monkeypatch) -> None:
    monkeypatch.setattr(capabilities.os, "name", "posix")
    monkeypatch.setattr(
        capabilities,
        "resolve_pty_executable",
        lambda name, search_path=None: f"/usr/bin/{name}",
    )
    monkeypatch.setattr(capabilities.shutil, "which", lambda *args, **kwargs: None)
    monkeypatch.setattr(capabilities, "resolve_generic_executable", lambda *args, **kwargs: None)

    disabled = capabilities.detect_capabilities(make_settings(), [])
    enabled = capabilities.detect_capabilities(make_settings(pty_allowed_executables="bash"), [])

    assert "command.pty" not in disabled
    assert "command.pty_output_lines" not in disabled
    assert "command.output_wait" not in disabled
    assert "command.session_list" not in disabled
    assert "command.pty" in enabled
    assert "command.pty_output_lines" in enabled
    assert "command.output_wait" in enabled
    assert "command.session_list" in enabled


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


def test_personal_mode_can_disable_session_signal_with_approval_requirement(tmp_path: Path) -> None:
    result = capabilities.detect_capabilities(
        make_settings(
            operation_mode="personal", personal_process_approval_required=True
        ),
        [tmp_path.resolve()],
    )
    assert "command.session_signal" not in result

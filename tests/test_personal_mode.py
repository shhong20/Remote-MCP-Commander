from __future__ import annotations

from pathlib import Path

import pytest

from remote_mcp_commander.agent.capabilities import detect_capabilities
from remote_mcp_commander.agent.executor import execute_argv
from remote_mcp_commander.config import Settings
from remote_mcp_commander.policy import validate_generic_argv


def test_hardened_mode_keeps_narrow_defaults() -> None:
    settings = Settings()

    assert settings.operation_mode == "hardened"
    assert settings.executable_allowlist == {"echo", "hostname", "whoami", "uptime"}
    assert settings.pty_executable_allowlist == set()
    assert settings.allowed_roots == []
    assert validate_generic_argv(["ls"], mode=settings.operation_mode) is not None


def test_personal_mode_enables_developer_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    settings = Settings(operation_mode="personal", allowed_roots_json="[]")

    assert {"bash", "ls", "git", "python3", "pytest"} <= settings.executable_allowlist
    assert {"bash", "sh", "python3"} <= settings.pty_executable_allowlist
    assert settings.allowed_roots == [str(Path.home())]
    assert validate_generic_argv(["bash", "-lc", "printf ok"], mode="personal") is None
    assert validate_generic_argv(["sudo", "true"], mode="personal") is not None


def test_personal_environment_keeps_useful_user_context_without_commander_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/example")
    monkeypatch.setenv("COMMANDER_AGENT_TOKEN", "do-not-forward-this-value")
    settings = Settings(operation_mode="personal")

    assert settings.command_environment["HOME"] == "/home/example"
    assert settings.command_environment["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert "COMMANDER_AGENT_TOKEN" not in settings.command_environment


@pytest.mark.asyncio
async def test_personal_execute_can_run_shell_pipeline_without_forwarding_agent_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/example")
    monkeypatch.setenv("COMMANDER_AGENT_TOKEN", "do-not-forward-this-value")
    settings = Settings(operation_mode="personal")

    result = await execute_argv(
        "req-personal",
        ["sh", "-c", 'printf "personal-ok:%s" "$HOME"; test -z "$COMMANDER_AGENT_TOKEN"'],
        allowlist=settings.executable_allowlist,
        timeout_s=2,
        max_output_bytes=1024,
        exec_search_path=settings.command_search_path,
        policy_mode=settings.operation_mode,
        child_env=settings.command_environment,
    )

    assert result.returncode == 0
    assert result.stdout == "personal-ok:/home/example"


def test_personal_capabilities_enable_command_pty_and_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    settings = Settings(operation_mode="personal")

    capabilities = detect_capabilities(settings, [Path.home()])

    assert "command.execute" in capabilities
    assert "command.session" in capabilities
    assert "command.pty" in capabilities
    assert "filesystem.discovery" in capabilities
    assert "file.read" in capabilities
    assert "file.write" in capabilities


@pytest.mark.parametrize(
    "name",
    [
        "chmod", "rsync", "ssh", "scp", "jq", "openssl", "nvidia-smi",
        "ffmpeg", "ffprobe", "sqlite3", "psql", "tmux", "screen", "watch",
        "sha256sum", "diff", "patch", "timeout",
    ],
)
def test_personal_mode_allows_common_developer_and_ops_profiles(name: str) -> None:
    assert validate_generic_argv([name], mode="personal") is None


@pytest.mark.parametrize("name", ["sudo", "kill", "pkill", "dd", "mkfs", "mount"])
def test_personal_mode_keeps_high_risk_or_duplicate_profiles_blocked(name: str) -> None:
    assert validate_generic_argv([name], mode="personal") is not None


def test_personal_mode_accepts_configured_custom_generic_profile() -> None:
    assert (
        validate_generic_argv(
            ["uv", "--version"], mode="personal", personal_allowlist={"uv"}
        )
        is None
    )


def test_hardened_mode_ignores_personal_custom_generic_profile() -> None:
    assert (
        validate_generic_argv(
            ["uv", "--version"], mode="hardened", personal_allowlist={"uv"}
        )
        is not None
    )

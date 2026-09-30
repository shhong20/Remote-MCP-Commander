import os

import pytest

from remote_mcp_commander.agent.env_policy import merge_command_env
from remote_mcp_commander.agent.executor import execute_argv


def test_personal_env_merge_preserves_base_and_overrides_values() -> None:
    merged = merge_command_env(
        {"PATH": "/usr/bin", "HOME": "/home/demo"},
        {"DEMO_FLAG": "enabled", "HOME": "/tmp/home"},
        personal_mode=True,
    )
    assert merged["PATH"] == "/usr/bin"
    assert merged["DEMO_FLAG"] == "enabled"
    assert merged["HOME"] == "/tmp/home"


def test_hardened_env_override_is_rejected() -> None:
    with pytest.raises(PermissionError, match="Personal mode"):
        merge_command_env({"PATH": "/usr/bin"}, {"DEMO": "1"}, personal_mode=False)


@pytest.mark.parametrize("key", ["COMMANDER_CONTROL_TOKEN", "LD_PRELOAD", "BASH_ENV", "ENV"])
def test_sensitive_env_keys_are_rejected(key: str) -> None:
    with pytest.raises(PermissionError, match="not allowed"):
        merge_command_env({"PATH": "/usr/bin"}, {key: "x"}, personal_mode=True)


def test_env_total_size_is_bounded() -> None:
    with pytest.raises(ValueError, match="total size"):
        merge_command_env(
            {"PATH": "/usr/bin"},
            {f"VAR_{index}": "x" * 4096 for index in range(5)},
            personal_mode=True,
        )


@pytest.mark.asyncio
async def test_personal_execute_receives_structured_env_override() -> None:
    result = await execute_argv(
        "env-ok",
        ["bash", "-lc", "printf %s \"$DEMO_FLAG\""],
        allowlist={"bash"},
        timeout_s=2,
        max_output_bytes=1024,
        exec_search_path=os.environ.get("PATH", "/usr/bin:/bin"),
        policy_mode="personal",
        child_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        env_overrides={"DEMO_FLAG": "structured-value"},
    )
    assert result.returncode == 0
    assert result.stdout == "structured-value"
    assert result.rejected is False


@pytest.mark.asyncio
async def test_hardened_execute_rejects_structured_env_override() -> None:
    result = await execute_argv(
        "env-denied",
        ["echo", "ok"],
        allowlist={"echo"},
        timeout_s=2,
        max_output_bytes=1024,
        env_overrides={"DEMO_FLAG": "structured-value"},
    )
    assert result.rejected is True
    assert "Personal mode" in (result.error or "")

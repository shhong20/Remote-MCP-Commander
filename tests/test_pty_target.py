import sys

import pytest

from remote_mcp_commander.policy import pty_approval_target
from remote_mcp_commander.pty_target import run


def test_pty_target_cli_hashes_exact_argv(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["remote-mcp-pty-target", "--", "bash", "-l"])
    run()
    assert capsys.readouterr().out.strip() == pty_approval_target(["bash", "-l"])


def test_pty_target_cli_rejects_absolute_executable(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["remote-mcp-pty-target", "--", "/bin/bash"])
    with pytest.raises(SystemExit, match="bare executable name"):
        run()


def test_pty_target_cli_binds_cwd(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["remote-mcp-pty-target", "--cwd", "/home/ubuntu/project", "--", "bash"],
    )
    run()
    assert capsys.readouterr().out.strip() == pty_approval_target(["bash"], "/home/ubuntu/project")

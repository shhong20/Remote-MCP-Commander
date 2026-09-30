import asyncio
import json

import pytest

from remote_mcp_commander.agent import one_shot
from remote_mcp_commander.agent.one_shot import OneShotCommandDispatcher
from remote_mcp_commander.protocol import CommandRequest, CommandResult


def make_dispatcher(max_active: int = 2) -> OneShotCommandDispatcher:
    return OneShotCommandDispatcher(
        allowlist={"echo"},
        timeout_s=2,
        max_output_bytes=1024,
        max_active=max_active,
        exec_search_path="/usr/bin:/bin",
        policy_mode="hardened",
        child_env={"PATH": "/usr/bin:/bin"},
    )


@pytest.mark.asyncio
async def test_submit_is_background_and_does_not_wait_for_command(monkeypatch) -> None:
    release = asyncio.Event()
    sent: list[str] = []

    async def slow_execute(request_id: str, argv: list[str], **kwargs) -> CommandResult:
        await release.wait()
        return CommandResult(request_id=request_id, returncode=0, stdout="done")

    async def send_text(payload: str) -> None:
        sent.append(payload)

    monkeypatch.setattr(one_shot, "execute_argv", slow_execute)
    dispatcher = make_dispatcher()
    accepted = await dispatcher.submit(
        CommandRequest(request_id="req-1", argv=["echo", "x"]), send_text
    )
    assert accepted is True
    assert dispatcher.active_count == 1
    assert sent == []

    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
        if dispatcher.active_count == 0:
            break
    assert dispatcher.active_count == 0
    assert json.loads(sent[0])["stdout"] == "done"


@pytest.mark.asyncio
async def test_dispatcher_rejects_when_active_limit_is_reached(monkeypatch) -> None:
    release = asyncio.Event()
    sent: list[str] = []

    async def slow_execute(request_id: str, argv: list[str], **kwargs) -> CommandResult:
        await release.wait()
        return CommandResult(request_id=request_id, returncode=0)

    async def send_text(payload: str) -> None:
        sent.append(payload)

    monkeypatch.setattr(one_shot, "execute_argv", slow_execute)
    dispatcher = make_dispatcher(max_active=1)
    await dispatcher.submit(CommandRequest(request_id="req-1", argv=["echo"]), send_text)
    accepted = await dispatcher.submit(CommandRequest(request_id="req-2", argv=["echo"]), send_text)
    assert accepted is False
    rejected = json.loads(sent[0])
    assert rejected["request_id"] == "req-2"
    assert rejected["rejected"] is True
    release.set()
    await dispatcher.cancel_all()


@pytest.mark.asyncio
async def test_cancel_all_cancels_active_execute(monkeypatch) -> None:
    cancelled = asyncio.Event()

    async def slow_execute(request_id: str, argv: list[str], **kwargs) -> CommandResult:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def send_text(payload: str) -> None:
        raise AssertionError("cancelled command must not send a result")

    monkeypatch.setattr(one_shot, "execute_argv", slow_execute)
    dispatcher = make_dispatcher()
    await dispatcher.submit(CommandRequest(request_id="req", argv=["echo"]), send_text)
    await asyncio.sleep(0)
    await dispatcher.cancel_all()
    assert cancelled.is_set()
    assert dispatcher.active_count == 0

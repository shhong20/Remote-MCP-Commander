import asyncio
import json

import pytest

from remote_mcp_commander.agent.line_wait_dispatcher import LineOutputWaitDispatcher
from remote_mcp_commander.protocol import (
    CommandSessionLineOutput,
    CommandSessionLineOutputRequest,
    PtySessionLineOutput,
    PtySessionLineOutputRequest,
)


class FakeCommandSessions:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def output_lines(self, request_id: str, session_id: str, **kwargs):
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return CommandSessionLineOutput(
            request_id=request_id, session_id=session_id,
            state="running", content="line\n", total_lines=1, next_line=1,
        )

class FakePtySessions:
    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def output_lines(self, request_id: str, session_id: str, **kwargs):
        await self.release.wait()
        return PtySessionLineOutput(
            request_id=request_id, session_id=session_id,
            state="running", content="pty\n", total_lines=1, next_line=1,
        )


async def wait_idle(dispatcher: LineOutputWaitDispatcher) -> None:
    for _ in range(20):
        if dispatcher.active_count == 0:
            return
        await asyncio.sleep(0)
    raise AssertionError("dispatcher did not become idle")


@pytest.mark.asyncio
async def test_command_wait_dispatches_in_background() -> None:
    commands = FakeCommandSessions()
    ptys = FakePtySessions()
    sent: list[str] = []
    dispatcher = LineOutputWaitDispatcher(commands, ptys)  # type: ignore[arg-type]
    request = CommandSessionLineOutputRequest(
        request_id="req", session_id="a" * 32, wait_ms=1000
    )

    async def send_text(payload: str) -> None:
        sent.append(payload)

    accepted = await dispatcher.submit_command(request, send_text)
    assert accepted is True
    assert dispatcher.active_count == 1
    assert sent == []

    commands.release.set()
    await wait_idle(dispatcher)
    assert json.loads(sent[0])["content"] == "line\n"


@pytest.mark.asyncio
async def test_wait_dispatcher_rejects_over_active_limit() -> None:
    commands = FakeCommandSessions()
    ptys = FakePtySessions()
    sent: list[str] = []
    dispatcher = LineOutputWaitDispatcher(
        commands, ptys, max_active=1  # type: ignore[arg-type]
    )

    async def send_text(payload: str) -> None:
        sent.append(payload)

    first = CommandSessionLineOutputRequest(
        request_id="one", session_id="b" * 32, wait_ms=1000
    )
    second = CommandSessionLineOutputRequest(
        request_id="two", session_id="c" * 32, wait_ms=1000
    )
    assert await dispatcher.submit_command(first, send_text) is True
    assert await dispatcher.submit_command(second, send_text) is False
    rejected = json.loads(sent[0])
    assert rejected["request_id"] == "two"
    assert rejected["rejected"] is True
    commands.release.set()
    await wait_idle(dispatcher)


@pytest.mark.asyncio
async def test_cancel_all_cancels_waiting_command() -> None:
    commands = FakeCommandSessions()
    ptys = FakePtySessions()
    dispatcher = LineOutputWaitDispatcher(commands, ptys)  # type: ignore[arg-type]

    async def send_text(payload: str) -> None:
        raise AssertionError("cancelled wait must not send a reply")

    request = CommandSessionLineOutputRequest(
        request_id="req", session_id="d" * 32, wait_ms=1000
    )
    await dispatcher.submit_command(request, send_text)
    await asyncio.sleep(0)
    await dispatcher.cancel_all()
    assert commands.cancelled.is_set()
    assert dispatcher.active_count == 0

@pytest.mark.asyncio
async def test_pty_wait_dispatches_in_background() -> None:
    commands = FakeCommandSessions()
    ptys = FakePtySessions()
    sent: list[str] = []
    dispatcher = LineOutputWaitDispatcher(commands, ptys)  # type: ignore[arg-type]
    request = PtySessionLineOutputRequest(
        request_id="pty", session_id="e" * 32, wait_ms=1000
    )

    async def send_text(payload: str) -> None:
        sent.append(payload)

    accepted = await dispatcher.submit_pty(request, send_text)
    assert accepted is True
    assert dispatcher.active_count == 1
    ptys.release.set()
    await wait_idle(dispatcher)
    assert json.loads(sent[0])["content"] == "pty\n"
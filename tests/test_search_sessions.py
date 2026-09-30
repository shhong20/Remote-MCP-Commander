from pathlib import Path

import pytest

from remote_mcp_commander.agent.search_session_ops import FileSearchSessionManager


@pytest.mark.asyncio
async def test_search_session_pages_results_and_stops(tmp_path: Path) -> None:
    for name in ["alpha-one.txt", "alpha-two.txt", "alpha-three.txt", "beta.txt"]:
        (tmp_path / name).write_text(name)
    manager = FileSearchSessionManager(roots=[tmp_path.resolve()])
    session_id = "a" * 32

    first = await manager.start(
        "req-1",
        session_id,
        str(tmp_path),
        "alpha",
        mode="files",
        file_glob=None,
        case_sensitive=False,
        page_size=2,
        max_results=10,
    )
    assert first.rejected is False
    assert first.returned_count == 2
    assert first.remaining == 1
    assert first.exhausted is False

    second = await manager.more("req-2", session_id, 2)
    assert second.returned_count == 1
    assert second.remaining == 0
    assert second.exhausted is True
    assert len({item.path for item in first.matches + second.matches}) == 3

    stopped = await manager.stop("req-3", session_id)
    assert stopped.stopped is True
    missing = await manager.more("req-4", session_id, 1)
    assert missing.rejected is True
    assert "not found" in (missing.error or "")


@pytest.mark.asyncio
async def test_search_session_preserves_content_match_order(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("needle first\nneedle second\n")
    (tmp_path / "b.txt").write_text("needle third\n")
    manager = FileSearchSessionManager(roots=[tmp_path.resolve()])
    session_id = "b" * 32

    first = await manager.start(
        "req-1",
        session_id,
        str(tmp_path),
        "needle",
        mode="content",
        file_glob="*.txt",
        case_sensitive=False,
        page_size=1,
        max_results=10,
    )
    second = await manager.more("req-2", session_id, 2)

    combined = first.matches + second.matches
    assert [item.line for item in combined] == [1, 2, 1]
    assert second.exhausted is True


@pytest.mark.asyncio
async def test_search_session_evicts_oldest_when_capacity_is_reached(tmp_path: Path) -> None:
    (tmp_path / "alpha.txt").write_text("alpha")
    manager = FileSearchSessionManager(roots=[tmp_path.resolve()], max_sessions=1)

    for session_id in ["c" * 32, "d" * 32]:
        result = await manager.start(
            "req",
            session_id,
            str(tmp_path),
            "alpha",
            mode="files",
            file_glob=None,
            case_sensitive=False,
            page_size=1,
            max_results=10,
        )
        assert result.rejected is False

    old = await manager.more("req-old", "c" * 32, 1)
    assert old.rejected is True
    current = await manager.more("req-current", "d" * 32, 1)
    assert current.rejected is False


def test_gateway_rejects_stateful_search_on_older_agent() -> None:
    from fastapi import HTTPException

    from remote_mcp_commander.config import Settings
    from remote_mcp_commander.gateway import app as gateway
    from remote_mcp_commander.protocol import FileSearchSessionStartBody

    connection = gateway.AgentConnection(websocket=object())  # type: ignore[arg-type]
    connection.capabilities = ["filesystem.search"]
    gateway.connections["server-01"] = connection
    settings = Settings(
        agent_token="agent-placeholder-value",
        control_token="control-placeholder-value",
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            import asyncio

            asyncio.run(
                gateway.start_file_search_session(
                    "server-01",
                    FileSearchSessionStartBody(root="/tmp", query="needle"),
                    settings,
                )
            )
        assert exc_info.value.status_code == 409
    finally:
        gateway.connections.pop("server-01", None)


@pytest.mark.asyncio
async def test_search_start_dispatcher_does_not_block_receive_loop(
    monkeypatch, tmp_path: Path
) -> None:
    import asyncio

    from remote_mcp_commander.agent.search_session_ops import FileSearchStartDispatcher
    from remote_mcp_commander.protocol import FileSearchSessionPage, FileSearchSessionStartRequest

    manager = FileSearchSessionManager(roots=[tmp_path.resolve()])
    release = asyncio.Event()
    sent: list[str] = []

    async def slow_start(*args, **kwargs):
        await release.wait()
        return FileSearchSessionPage(
            request_id=args[0],
            session_id=args[1],
            exhausted=True,
        )

    async def send_text(payload: str) -> None:
        sent.append(payload)

    monkeypatch.setattr(manager, "start", slow_start)
    dispatcher = FileSearchStartDispatcher(manager, max_active=1)
    request = FileSearchSessionStartRequest(
        request_id="req",
        session_id="f" * 32,
        root=str(tmp_path),
        query="needle",
    )
    accepted = await dispatcher.submit(request, send_text)
    assert accepted is True
    assert dispatcher._tasks
    assert sent == []
    release.set()
    await asyncio.gather(*list(dispatcher._tasks))
    assert sent


@pytest.mark.asyncio
async def test_search_start_dispatcher_rejects_excess_active_searches(
    monkeypatch, tmp_path: Path
) -> None:
    import asyncio
    import json

    from remote_mcp_commander.agent.search_session_ops import FileSearchStartDispatcher
    from remote_mcp_commander.protocol import FileSearchSessionPage, FileSearchSessionStartRequest

    manager = FileSearchSessionManager(roots=[tmp_path.resolve()])
    release = asyncio.Event()
    sent: list[str] = []

    async def slow_start(*args, **kwargs):
        await release.wait()
        return FileSearchSessionPage(
            request_id=args[0],
            session_id=args[1],
            exhausted=True,
        )

    async def send_text(payload: str) -> None:
        sent.append(payload)

    monkeypatch.setattr(manager, "start", slow_start)
    dispatcher = FileSearchStartDispatcher(manager, max_active=1)
    first = FileSearchSessionStartRequest(
        request_id="first",
        session_id="1" * 32,
        root=str(tmp_path),
        query="needle",
    )
    second = FileSearchSessionStartRequest(
        request_id="second",
        session_id="2" * 32,
        root=str(tmp_path),
        query="needle",
    )
    assert await dispatcher.submit(first, send_text) is True
    assert await dispatcher.submit(second, send_text) is False
    rejected = json.loads(sent[-1])
    assert rejected["rejected"] is True
    assert "too many active" in rejected["error"]
    release.set()
    await asyncio.gather(*list(dispatcher._tasks))


@pytest.mark.asyncio
async def test_stateful_search_skips_hidden_paths_by_default(tmp_path: Path) -> None:
    (tmp_path / "alpha-visible.txt").write_text("visible")
    (tmp_path / ".alpha-hidden.txt").write_text("hidden")
    hidden_dir = tmp_path / ".venv"
    hidden_dir.mkdir()
    (hidden_dir / "alpha-inside.txt").write_text("hidden-dir")
    manager = FileSearchSessionManager(roots=[tmp_path.resolve()])

    default_page = await manager.start(
        "req-default",
        "7" * 32,
        str(tmp_path),
        "alpha",
        mode="files",
        file_glob=None,
        case_sensitive=False,
        page_size=10,
        max_results=20,
    )
    assert [Path(item.path).name for item in default_page.matches] == ["alpha-visible.txt"]

    hidden_page = await manager.start(
        "req-hidden",
        "8" * 32,
        str(tmp_path),
        "alpha",
        mode="files",
        file_glob=None,
        case_sensitive=False,
        page_size=10,
        max_results=20,
        include_hidden=True,
    )
    assert {Path(item.path).name for item in hidden_page.matches} == {
        ".alpha-hidden.txt",
        "alpha-inside.txt",
        "alpha-visible.txt",
    }

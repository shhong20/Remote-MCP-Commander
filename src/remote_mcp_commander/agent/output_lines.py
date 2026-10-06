from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class LinePage:
    content: str
    total_lines: int
    start_line: int
    next_line: int
    eof: bool
    pending_partial: bool


def _lf_lines(text: str, *, include_partial: bool) -> tuple[list[str], bool]:
    if not text:
        return [], False
    parts = text.split("\n")
    ends_with_lf = text.endswith("\n")
    complete = parts[:-1]
    lines = [part + "\n" for part in complete]
    pending_partial = not ends_with_lf
    if pending_partial and include_partial:
        lines.append(parts[-1])
    return lines, pending_partial


def paginate_lines(
    text: str,
    *,
    running: bool,
    offset: int,
    max_lines: int,
) -> LinePage:
    lines, has_partial = _lf_lines(text, include_partial=not running)
    total = len(lines)
    if offset < 0:
        start = max(0, total + offset)
        selected = lines[start:]
    else:
        start = min(offset, total)
        selected = lines[start : start + max_lines]
    next_line = start + len(selected)
    return LinePage(
        content="".join(selected),
        total_lines=total,
        start_line=start,
        next_line=next_line,
        eof=not running and next_line >= total,
        pending_partial=running and has_partial,
    )


async def wait_for_line_page(
    get_text: Callable[[], str],
    is_running: Callable[[], bool],
    can_grow: Callable[[], bool],
    output_event: asyncio.Event,
    *,
    offset: int,
    max_lines: int,
    wait_ms: int,
) -> tuple[LinePage, int, bool]:
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + wait_ms / 1000.0
    wait_timed_out = False

    while True:
        running = is_running()
        page = paginate_lines(
            get_text(), running=running, offset=offset, max_lines=max_lines
        )
        if (
            wait_ms <= 0
            or offset < 0
            or page.content
            or not running
            or not can_grow()
        ):
            break

        remaining = deadline - loop.time()
        if remaining <= 0:
            wait_timed_out = True
            break

        output_event.clear()
        running = is_running()
        page = paginate_lines(
            get_text(), running=running, offset=offset, max_lines=max_lines
        )
        if page.content or not running or not can_grow():
            break
        try:
            await asyncio.wait_for(output_event.wait(), timeout=remaining)
        except TimeoutError:
            wait_timed_out = True
            running = is_running()
            page = paginate_lines(
                get_text(), running=running, offset=offset, max_lines=max_lines
            )
            break

    waited_ms = max(0, int((loop.time() - started) * 1000)) if wait_ms else 0
    return page, waited_ms, wait_timed_out

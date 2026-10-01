from __future__ import annotations

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

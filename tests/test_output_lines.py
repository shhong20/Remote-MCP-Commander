from remote_mcp_commander.agent.output_lines import paginate_lines


def test_line_pager_uses_lf_only_and_preserves_carriage_returns() -> None:
    page = paginate_lines(
        "progress-1\rprogress-2\ndone", running=False, offset=0, max_lines=10
    )
    assert page.content == "progress-1\rprogress-2\ndone"
    assert page.total_lines == 2
    assert page.eof is True
    assert page.pending_partial is False


def test_line_pager_hides_running_partial_and_supports_tail() -> None:
    running = paginate_lines("one\npartial\r", running=True, offset=0, max_lines=10)
    assert running.content == "one\n"
    assert running.total_lines == 1
    assert running.pending_partial is True
    assert running.eof is False

    tail = paginate_lines("one\ntwo\nthree\n", running=False, offset=-2, max_lines=1)
    assert tail.content == "two\nthree\n"
    assert tail.start_line == 1
    assert tail.next_line == 3
    assert tail.eof is True

from __future__ import annotations

from remote_mcp_commander.gateway.audit import AuditJournal


def test_usage_stats_summarizes_results_latency_and_agent_filter(tmp_path) -> None:
    journal = AuditJournal(tmp_path / "audit.jsonl")
    records = [
        {
            "event": "command_requested",
            "agent_id": "server-01",
            "request_id": "req-1",
            "ts": "2026-10-02T00:00:00.000+00:00",
        },
        {
            "event": "command_completed",
            "agent_id": "server-01",
            "request_id": "req-1",
            "returncode": 0,
            "rejected": False,
            "timed_out": False,
            "ts": "2026-10-02T00:00:00.100+00:00",
        },
        {
            "event": "command_requested",
            "agent_id": "server-01",
            "request_id": "req-2",
            "ts": "2026-10-02T00:00:01.000+00:00",
        },
        {
            "event": "command_completed",
            "agent_id": "server-01",
            "request_id": "req-2",
            "returncode": 7,
            "rejected": False,
            "timed_out": False,
            "ts": "2026-10-02T00:00:01.300+00:00",
        },
        {
            "event": "file_read",
            "agent_id": "server-01",
            "rejected": False,
            "ts": "2026-10-02T00:00:02.000+00:00",
        },
        {
            "event": "command_denied",
            "agent_id": "server-01",
            "ts": "2026-10-02T00:00:03.000+00:00",
        },
        {
            "event": "command_completed",
            "agent_id": "server-02",
            "request_id": "other",
            "returncode": 0,
            "rejected": False,
            "timed_out": False,
            "ts": "2026-10-02T00:00:04.000+00:00",
        },
    ]
    for record in records:
        journal.append(record)

    result = journal.usage_stats(agent_id="server-01", max_scan_bytes=100_000)

    assert result.records_scanned == 6
    assert result.result_records == 4
    assert result.successful_results == 2
    assert result.failed_results == 2
    assert result.rejected_results == 1
    assert result.timed_out_results == 0
    assert result.success_rate_pct == 50.0
    assert result.latency_samples == 2
    assert result.latency_avg_ms == 200.0
    assert result.latency_p50_ms == 100.0
    assert result.latency_p95_ms == 300.0
    assert result.window_started_at is not None
    assert result.window_ended_at is not None
    counts = {item.event: item.count for item in result.events}
    assert counts["command_requested"] == 2
    assert counts["command_completed"] == 2
    assert counts["file_read"] == 1
    assert counts["command_denied"] == 1
    assert result.scan_truncated is False


def test_usage_stats_reports_scan_truncation_without_exposing_payloads(tmp_path) -> None:
    journal = AuditJournal(tmp_path / "audit.jsonl")
    for index in range(100):
        journal.append(
            {
                "event": "file_write",
                "agent_id": "server-01",
                "rejected": False,
                "path": f"/private/{index}",
                "content": "secret-" + "x" * 120,
                "ts": "2026-10-02T00:00:00+00:00",
            }
        )

    result = journal.usage_stats(agent_id="server-01", max_scan_bytes=4096)

    assert result.scan_truncated is True
    assert result.records_scanned > 0
    assert result.result_records == result.successful_results
    assert not hasattr(result, "records")
    assert all("private" not in item.event for item in result.events)

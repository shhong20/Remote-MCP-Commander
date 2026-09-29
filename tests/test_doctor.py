import json
from pathlib import Path

from remote_mcp_commander.config import Settings
from remote_mcp_commander.doctor import check_agent, check_gateway, check_mcp, main


def secure_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "agent_token": "a" * 32,
        "control_token": "c" * 32,
        "approval_admin_token": "p" * 32,
        "mcp_token": "m" * 32,
    }
    values.update(overrides)
    return Settings(**values)


def failures(checks) -> list[str]:
    return [check.name for check in checks if check.status == "fail"]


def test_gateway_doctor_accepts_secure_writable_state_paths(tmp_path: Path) -> None:
    settings = secure_settings(
        audit_path=str(tmp_path / "audit.jsonl"),
        registry_path=str(tmp_path / "registry.json"),
        bind_host="127.0.0.1",
    )

    checks = check_gateway(settings)

    assert failures(checks) == []


def test_gateway_doctor_fails_placeholder_control_credential(tmp_path: Path, capsys) -> None:
    settings = Settings(
        control_token="change-me-control-token",
        audit_path=str(tmp_path / "audit.jsonl"),
        registry_path=str(tmp_path / "registry.json"),
    )

    exit_code = main(["gateway", "--json"], settings=settings)
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["failures"] >= 1


def test_agent_doctor_rejects_gateway_path_for_another_agent(tmp_path: Path) -> None:
    settings = Settings(
        agent_id="server-01",
        agent_token="agent-secret-value-123456",
        gateway_ws="ws://127.0.0.1:8765/ws/agent/server-02",
        allowed_roots_json=json.dumps([str(tmp_path)]),
    )

    results = check_agent(settings)
    security = next(result for result in results if result.name == "agent.security")
    assert security.status == "fail"
    assert "does not match configured Agent ID" in security.detail


def test_mcp_doctor_rejects_streamable_http_without_mcp_token() -> None:
    settings = secure_settings(
        mcp_transport="streamable-http",
        mcp_token="",
        gateway_http="http://127.0.0.1:8765",
        mcp_host="127.0.0.1",
    )

    checks = check_mcp(settings)

    assert "mcp.http_security" in failures(checks)


def test_agent_doctor_validates_identity_and_allowed_roots(tmp_path: Path) -> None:
    settings = secure_settings(
        agent_id="server-01",
        gateway_ws="ws://127.0.0.1:8765/ws/agent/server-01",
        allowed_roots_json=json.dumps([str(tmp_path)]),
    )

    checks = check_agent(settings)

    assert failures(checks) == []
    names = {check.name for check in checks}
    assert "agent.allowed_roots" in names
    assert "agent.capabilities" in names


def test_agent_doctor_rejects_gateway_path_for_different_agent() -> None:
    settings = secure_settings(
        agent_id="server-02",
        gateway_ws="wss://gateway.example.test/ws/agent/server-01",
    )

    checks = check_agent(settings)

    assert "agent.security" in failures(checks)


def test_doctor_json_output_never_contains_credentials(tmp_path: Path, capsys) -> None:
    control = "CONTROL_SECRET_1234567890"
    approval = "APPROVAL_SECRET_123456789"
    settings = secure_settings(
        control_token=control,
        approval_admin_token=approval,
        audit_path=str(tmp_path / "audit.jsonl"),
        registry_path=str(tmp_path / "registry.json"),
    )

    returncode = main(["gateway", "--json"], settings=settings)
    output = capsys.readouterr().out

    assert returncode == 0
    assert control not in output
    assert approval not in output
    payload = json.loads(output)
    assert payload["ok"] is True


def test_agent_doctor_warns_when_filesystem_roots_are_disabled() -> None:
    settings = Settings(
        agent_id="server-01",
        agent_token="agent-placeholder-value",
        gateway_ws="ws://127.0.0.1:8765/ws/agent/server-01",
        allowed_roots_json="[]",
    )

    results = check_agent(settings)
    roots = next(result for result in results if result.name == "agent.allowed_roots")
    assert roots.status == "warn"
    assert "filesystem and Git tools stay disabled" in roots.detail


def test_mcp_doctor_rejects_public_plain_http_bind(capsys) -> None:
    settings = Settings(
        control_token="control-placeholder-value",
        gateway_http="http://127.0.0.1:8765",
        mcp_transport="streamable-http",
        mcp_host="0.0.0.0",
        mcp_token="mcp-placeholder-value",
        mcp_issuer_url="https://mcp.example.test",
        mcp_resource_url="https://mcp.example.test/mcp",
    )

    exit_code = main(["mcp", "--json"], settings=settings)
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["failures"] >= 1

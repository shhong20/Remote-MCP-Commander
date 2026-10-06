from pathlib import Path

import pytest
from fastapi import HTTPException

from remote_mcp_commander.config import Settings, apply_operator_overrides
from remote_mcp_commander.gateway import app as gateway
from remote_mcp_commander.operator_config import read_operator_overrides
from remote_mcp_commander.protocol import OperatorConfigSetBody


def make_settings(path: Path, *, mode: str = "personal", **overrides: object) -> Settings:
    values: dict[str, object] = {
        "agent_token": "agent-placeholder-value",
        "control_token": "control-placeholder-value",
        "operation_mode": mode,
        "operator_config_path": str(path),
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_operator_config_set_is_audited_and_requires_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "operator.json"
    settings = make_settings(path)
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        gateway, "audit_required", lambda event, **fields: events.append((event, fields))
    )
    monkeypatch.setattr(gateway, "audit", lambda event, **fields: events.append((event, fields)))

    result = await gateway.set_operator_config(
        OperatorConfigSetBody(key="max_output_bytes", value=131_072), settings
    )

    assert read_operator_overrides(path) == {"max_output_bytes": 131_072}
    assert result.snapshot.effective["max_output_bytes"] == 65_536
    assert result.snapshot.desired["max_output_bytes"] == 131_072
    assert result.snapshot.restart_required is True
    assert [item[0] for item in events] == [
        "operator_config_update_requested",
        "operator_config_updated",
    ]
    assert "control_token" not in result.snapshot.model_dump()
    assert "agent_token" not in result.snapshot.model_dump()


@pytest.mark.asyncio
async def test_operator_config_reset_returns_to_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "operator.json"
    base = make_settings(path)
    first = {"max_output_bytes": 131_072}
    from remote_mcp_commander.operator_config import write_operator_overrides

    write_operator_overrides(path, first)
    running = apply_operator_overrides(base, first)
    monkeypatch.setattr(gateway, "audit_required", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway, "audit", lambda *args, **kwargs: None)

    result = await gateway.set_operator_config(
        OperatorConfigSetBody(key="max_output_bytes", value=None), running
    )

    assert read_operator_overrides(path) == {}
    assert result.removed is True
    assert result.snapshot.effective["max_output_bytes"] == 131_072
    assert result.snapshot.desired["max_output_bytes"] == 65_536
    assert result.snapshot.restart_required is True


@pytest.mark.asyncio
async def test_operator_config_change_requires_personal_mode(tmp_path: Path) -> None:
    path = tmp_path / "operator.json"
    settings = make_settings(path, mode="hardened")

    with pytest.raises(HTTPException) as exc_info:
        await gateway.set_operator_config(
            OperatorConfigSetBody(key="max_output_bytes", value=131_072), settings
        )

    assert exc_info.value.status_code == 403
    assert not path.exists()


@pytest.mark.asyncio
async def test_operator_config_rejects_wrong_numeric_type_before_audit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "operator.json"
    settings = make_settings(path)
    called = False

    def required(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(gateway, "audit_required", required)

    with pytest.raises(HTTPException) as exc_info:
        await gateway.set_operator_config(
            OperatorConfigSetBody(key="session_max_active", value=4.5), settings
        )

    assert exc_info.value.status_code == 422
    assert called is False
    assert not path.exists()


@pytest.mark.asyncio
async def test_operator_config_required_audit_failure_prevents_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "operator.json"
    settings = make_settings(path)

    def fail_audit(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(gateway, "audit_required", fail_audit)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        await gateway.set_operator_config(
            OperatorConfigSetBody(key="max_output_bytes", value=131_072), settings
        )

    assert not path.exists()

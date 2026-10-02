import os
from pathlib import Path

import pytest

from remote_mcp_commander.config import get_settings
from remote_mcp_commander.operator_config import (
    read_operator_overrides,
    write_operator_overrides,
)


def test_operator_config_round_trip_is_private(tmp_path: Path) -> None:
    path = tmp_path / "operator.json"
    values = {
        "session_max_active": 6,
        "exec_timeout_s": 12.5,
        "transfer_max_bytes": 32 * 1024 * 1024,
    }

    write_operator_overrides(path, values)

    assert read_operator_overrides(path) == values
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_operator_config_rejects_unknown_boolean_and_insecure_file(tmp_path: Path) -> None:
    path = tmp_path / "operator.json"
    path.write_text('{"allowed_roots_json":1}\n', encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="not mutable"):
        read_operator_overrides(path)

    path.write_text('{"session_max_active":true}\n', encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="not boolean"):
        read_operator_overrides(path)

    if os.name != "nt":
        path.write_text('{"session_max_active":4}\n', encoding="utf-8")
        path.chmod(0o644)
        with pytest.raises(ValueError, match="group/world"):
            read_operator_overrides(path)


def test_operator_config_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "operator.json"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="non-symlink"):
        read_operator_overrides(link)
    with pytest.raises(ValueError, match="symlink"):
        write_operator_overrides(link, {"session_max_active":4})


def test_get_settings_applies_operator_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "operator.json"
    write_operator_overrides(
        path,
        {
            "session_max_active": 7,
            "session_history_limit": 50,
            "transfer_session_ttl_s": 1200,
        },
    )
    monkeypatch.setenv("COMMANDER_OPERATOR_CONFIG_PATH", str(path))
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.session_max_active == 7
        assert settings.session_history_limit == 50
        assert settings.transfer_session_ttl_s == 1200
    finally:
        get_settings.cache_clear()


def test_get_settings_fails_closed_on_invalid_cross_field_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "operator.json"
    write_operator_overrides(
        path,
        {"session_max_active":20, "session_history_limit":10},
    )
    monkeypatch.setenv("COMMANDER_OPERATOR_CONFIG_PATH", str(path))
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="history limit"):
            get_settings()
    finally:
        get_settings.cache_clear()

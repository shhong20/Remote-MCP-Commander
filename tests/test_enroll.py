import os
import stat

import pytest

from remote_mcp_commander.agent.enroll import (
    require_secure_enrollment_url,
    store_agent_token,
)


def test_remote_enrollment_requires_https() -> None:
    require_secure_enrollment_url("https://gateway.example.test")
    require_secure_enrollment_url("http://127.0.0.1:8765")
    require_secure_enrollment_url("http://localhost:8765")

    with pytest.raises(ValueError, match="HTTPS"):
        require_secure_enrollment_url("http://gateway.example.test")


def test_store_agent_token_round_trip(tmp_path) -> None:
    path = tmp_path / "agent-token"
    store_agent_token(path, "local-test-credential-value")
    assert path.read_text(encoding="utf-8").strip() == "local-test-credential-value"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file mode assertion")
def test_store_agent_token_is_owner_only(tmp_path) -> None:
    path = tmp_path / "agent-token"
    store_agent_token(path, "local-test-credential-value")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

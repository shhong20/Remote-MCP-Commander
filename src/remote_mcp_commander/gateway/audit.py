from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
from typing import Any


audit_logger = logging.getLogger("remote_mcp_commander.audit")


def audit(event: str, **fields: Any) -> None:
    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    audit_logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))

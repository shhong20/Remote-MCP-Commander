from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

audit_logger = logging.getLogger("remote_mcp_commander.audit")


def audit(event: str, **fields: Any) -> None:
    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    audit_logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))

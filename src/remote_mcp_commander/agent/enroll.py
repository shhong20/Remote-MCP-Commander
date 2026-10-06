from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse

import httpx

from remote_mcp_commander.config import get_settings
from remote_mcp_commander.protocol import EnrollmentClaimResult


def require_secure_enrollment_url(gateway_url: str) -> None:
    parsed = urlparse(gateway_url)
    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and parsed.hostname in loopback_hosts:
        return
    raise ValueError("remote enrollment requires an HTTPS Gateway URL")


def store_agent_token(path: Path, token: str) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)
        handle.write("\n")
    os.replace(temp_path, path)
    if os.name != "nt":
        os.chmod(path, 0o600)


async def enroll_agent(
    *,
    gateway_url: str,
    agent_id: str,
    code: str,
    token_path: Path,
) -> Path:
    require_secure_enrollment_url(gateway_url)
    async with httpx.AsyncClient(base_url=gateway_url, timeout=15.0) as client:
        response = await client.post(
            "/api/v1/enrollments/claim",
            json={"agent_id": agent_id, "code": code},
        )
    if response.is_error:
        try:
            detail = str(response.json().get("detail", "enrollment failed"))
        except ValueError:
            detail = "enrollment failed"
        raise RuntimeError(f"Gateway enrollment failed ({response.status_code}): {detail}")

    result = EnrollmentClaimResult.model_validate(response.json())
    store_agent_token(token_path, result.agent_token)
    return token_path.expanduser()


def run() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Enroll a Remote MCP Commander Agent")
    parser.add_argument("--code", required=True, help="one-time pairing code from the Gateway")
    parser.add_argument("--agent-id", default=settings.agent_id)
    parser.add_argument("--gateway", default=settings.gateway_http)
    parser.add_argument("--token-file", default=str(settings.agent_token_path))
    args = parser.parse_args()

    path = asyncio.run(
        enroll_agent(
            gateway_url=args.gateway,
            agent_id=args.agent_id,
            code=args.code,
            token_path=Path(args.token_file),
        )
    )
    print(f"Enrolled {args.agent_id}; credential stored at {path}")


if __name__ == "__main__":
    run()

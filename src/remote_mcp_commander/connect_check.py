"""Read-only online checks for a deployed ChatGPT/Auth0 MCP connection.

No login, token issuance, or changes to the provider/server are performed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from urllib.parse import urlparse

import httpx

from remote_mcp_commander.config import Settings, get_settings


async def check_connection(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> list[dict[str, str]]:
    settings.validate_mcp_gateway_security()
    settings.validate_mcp_http_security()
    if settings.mcp_transport != "streamable-http" or settings.mcp_auth_mode != "oauth":
        raise ValueError("ChatGPT checks require Streamable HTTP with OAuth authentication")
    results: list[dict[str, str]] = []

    async with httpx.AsyncClient(
        transport=transport,
        timeout=settings.mcp_oauth_timeout_s,
        follow_redirects=False,
        trust_env=False,
    ) as client:

        async def fetch(url: str) -> dict:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 1_048_576:
                        raise ValueError("response exceeds limit")
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError("expected JSON object")
            return value

        async def check(name: str, operation, guidance: str) -> None:
            try:
                await operation()
                results.append({"name": name, "status": "pass", "detail": "verified"})
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                results.append({"name": name, "status": "fail", "detail": guidance})

        resource = urlparse(settings.mcp_resource_url)
        metadata_url = (
            f"{resource.scheme}://{resource.netloc}"
            f"/.well-known/oauth-protected-resource{resource.path}"
        )

        async def resource_metadata() -> None:
            data = await fetch(metadata_url)
            if (
                data.get("resource") != settings.mcp_resource_url
                or settings.mcp_issuer_url not in data.get("authorization_servers", [])
                or settings.mcp_scope not in data.get("scopes_supported", [])
            ):
                raise ValueError("resource metadata mismatch")

        async def challenge() -> None:
            response = await client.post(
                settings.mcp_resource_url,
                headers={"Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            expected = f'resource_metadata="{metadata_url}"'
            if response.status_code != 401 or expected not in response.headers.get(
                "www-authenticate", ""
            ):
                raise ValueError("missing OAuth authentication challenge")

        async def provider_metadata() -> None:
            data = await fetch(
                settings.mcp_issuer_url.rstrip("/") + "/.well-known/openid-configuration"
            )
            if (
                data.get("issuer") != settings.mcp_issuer_url
                or "S256" not in data.get("code_challenge_methods_supported", [])
                or data.get("jwks_uri")
                != settings.mcp_issuer_url.rstrip("/") + "/.well-known/jwks.json"
            ):
                raise ValueError("provider discovery mismatch")
            for name in ("authorization_endpoint", "token_endpoint"):
                endpoint = urlparse(data[name])
                if (
                    endpoint.scheme != "https"
                    or not endpoint.hostname
                    or endpoint.username is not None
                    or endpoint.password is not None
                    or endpoint.fragment
                ):
                    raise ValueError("invalid provider endpoint")

        async def signing_keys() -> None:
            data = await fetch(settings.mcp_issuer_url.rstrip("/") + "/.well-known/jwks.json")
            keys = data.get("keys")
            if not isinstance(keys, list) or not any(
                isinstance(key, dict)
                and key.get("kty") == "RSA"
                and key.get("use", "sig") == "sig"
                and key.get("alg", "RS256") == "RS256"
                and key.get("kid")
                for key in keys
            ):
                raise ValueError("no RS256 signing keys")

        await check("mcp.resource_metadata", resource_metadata, "check TLS, proxy and resource URL")
        await check(
            "mcp.auth_challenge",
            challenge,
            "MCP must reject anonymous requests with OAuth discovery",
        )
        await check(
            "oauth.discovery",
            provider_metadata,
            "check exact Auth0 issuer, PKCE S256 and endpoints",
        )
        await check("oauth.jwks", signing_keys, "check Auth0 JWKS reachability and RS256 keys")
    results.append(
        {
            "name": "chatgpt.login_and_tools",
            "status": "warn",
            "detail": "not tested: complete ChatGPT login, tools/list and a read-only device call",
        }
    )
    return results


def main(argv: list[str] | None = None, *, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit structured check results")
    args = parser.parse_args(argv)
    try:
        results = asyncio.run(check_connection(settings or get_settings()))
    except ValueError:
        results = [
            {
                "name": "configuration",
                "status": "fail",
                "detail": "run remote-mcp-doctor mcp and configure OAuth first",
            }
        ]
    if args.json:
        print(json.dumps({"checks": results}, ensure_ascii=False))
    else:
        for result in results:
            print(f"{result['status'].upper()} {result['name']}: {result['detail']}")
    return int(any(result["status"] == "fail" for result in results))


def run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    run()

import json

import httpx
import pytest
from test_oauth import oauth_settings

from remote_mcp_commander.connect_check import check_connection, main


def connection_transport(*, broken: str = "") -> httpx.MockTransport:
    def handler(request):
        assert "authorization" not in request.headers
        path = request.url.path
        if path == broken:
            return httpx.Response(503, text="PROVIDER_SECRET_MUST_NOT_APPEAR")
        if path == "/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(
                200,
                json={
                    "resource": "https://mcp.example.test/mcp",
                    "authorization_servers": ["https://tenant.auth0.test/"],
                    "scopes_supported": ["commander:use"],
                },
            )
        if path == "/mcp":
            assert request.method == "POST"
            return httpx.Response(
                401,
                headers={
                    "www-authenticate": 'Bearer resource_metadata="https://mcp.example.test/.well-known/oauth-protected-resource/mcp"',
                },
            )
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": "https://tenant.auth0.test/",
                    "authorization_endpoint": "https://tenant.auth0.test/authorize",
                    "token_endpoint": "https://tenant.auth0.test/oauth/token",
                    "jwks_uri": "https://tenant.auth0.test/.well-known/jwks.json",
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if path == "/.well-known/jwks.json":
            return httpx.Response(
                200,
                json={
                    "keys": [
                        {
                            "kty": "RSA",
                            "kid": "key-1",
                            "alg": "RS256",
                            "use": "sig",
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected endpoint {path}")

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_checks_public_discovery_without_claiming_live_login():
    results = await check_connection(oauth_settings(), transport=connection_transport())
    assert [item["status"] for item in results] == ["pass"] * 4 + ["warn"]
    assert "not tested" in results[-1]["detail"]


@pytest.mark.parametrize(
    "path",
    [
        "/.well-known/oauth-protected-resource/mcp",
        "/mcp",
        "/.well-known/openid-configuration",
        "/.well-known/jwks.json",
    ],
)
@pytest.mark.asyncio
async def test_errors_fail_safely_without_provider_body(path):
    results = await check_connection(oauth_settings(), transport=connection_transport(broken=path))
    assert sum(item["status"] == "fail" for item in results) == 1
    assert "PROVIDER_SECRET" not in json.dumps(results)


@pytest.mark.asyncio
async def test_rejects_wrong_resource_metadata_and_missing_pkce():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    results = await check_connection(oauth_settings(), transport=transport)
    assert [item["status"] for item in results] == ["fail"] * 4 + ["warn"]


def test_cli_configuration_failure_is_redacted(capsys):
    settings = oauth_settings(mcp_oauth_subject="", control_token="SECRET")
    assert main(["--json"], settings=settings) == 1
    output = capsys.readouterr().out
    assert "SECRET" not in output
    assert json.loads(output)["checks"][0]["status"] == "fail"


@pytest.mark.asyncio
async def test_static_mode_is_not_reported_as_chatgpt_ready():
    settings = oauth_settings(mcp_auth_mode="static", mcp_token="m" * 32)
    with pytest.raises(ValueError, match="OAuth"):
        await check_connection(settings, transport=connection_transport())

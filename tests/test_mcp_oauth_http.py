import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from test_oauth import access_token, jwk, oauth_settings

from remote_mcp_commander.gateway.client import GatewayClient
from remote_mcp_commander.mcp_server import build_mcp, http_transport_security
from remote_mcp_commander.oauth import OAuthTokenVerifier


@pytest.mark.asyncio
async def test_http_oauth_discovery_auth_and_read_only_tool(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = oauth_settings(mcp_token="")
    verifier = OAuthTokenVerifier(
        settings,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"keys": [jwk(key)]})
        ),
    )
    monkeypatch.setattr("remote_mcp_commander.mcp_server.OAuthTokenVerifier", lambda _: verifier)
    gateway_calls = []

    def gateway(request):
        assert request.headers["authorization"] == "Bearer " + settings.control_token
        assert request.url.path == "/api/v1/agents"
        gateway_calls.append(request)
        return httpx.Response(200, json={"agents": []})

    monkeypatch.setattr(
        "remote_mcp_commander.mcp_server.GatewayClient",
        lambda _: GatewayClient(settings, transport=httpx.MockTransport(gateway)),
    )
    app = build_mcp(settings).streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=http_transport_security(settings),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://mcp.example.test",
            headers={"Accept": "application/json, text/event-stream"},
        ) as client:
            metadata = await client.get("/.well-known/oauth-protected-resource/mcp")
            assert metadata.status_code == 200
            assert metadata.json()["resource"] == settings.mcp_resource_url
            assert metadata.json()["authorization_servers"] == [settings.mcp_issuer_url]
            request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            response = await client.post("/mcp", json=request)
            assert response.status_code == 401
            assert "resource_metadata=" in response.headers["www-authenticate"]
            for token in ["legacy-token-not-accepted", access_token(key, sub="auth0|other")]:
                response = await client.post(
                    "/mcp", json=request, headers={"Authorization": "Bearer " + token}
                )
                assert response.status_code == 401
            client.headers["Authorization"] = "Bearer " + access_token(key)
            init = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "oauth-test", "version": "1"},
                    },
                },
            )
            assert init.status_code == 200
            client.headers["MCP-Protocol-Version"] = init.json()["result"]["protocolVersion"]
            response = await client.post("/mcp", json=request)
            assert response.status_code == 200
            assert "list_devices" in {tool["name"] for tool in response.json()["result"]["tools"]}
            call = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "list_devices", "arguments": {}},
                },
            )
            assert call.status_code == 200
            assert not call.json()["result"].get("isError")
            assert len(gateway_calls) == 1
            wrong_host = await client.post("/mcp", json=request, headers={"Host": "evil.test"})
            assert wrong_host.status_code == 421
            wrong_origin = await client.post(
                "/mcp", json=request, headers={"Origin": "https://evil.test"}
            )
            assert wrong_origin.status_code == 403

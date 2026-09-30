import asyncio
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from remote_mcp_commander.config import Settings
from remote_mcp_commander.oauth import OAuthTokenVerifier


def oauth_settings(**overrides: object) -> Settings:
    values = dict(
        _env_file=None,
        control_token="c" * 32,
        mcp_transport="streamable-http",
        mcp_auth_mode="oauth",
        mcp_token="legacy-token-not-accepted",
        mcp_issuer_url="https://tenant.auth0.test/",
        mcp_resource_url="https://mcp.example.test/mcp",
        mcp_oauth_subject="auth0|owner",
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk(key, kid="key-1") -> dict:
    value = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    value.update(kid=kid, use="sig", alg="RS256")
    return value


def access_token(key, *, kid="key-1", remove=(), **overrides) -> str:
    now = int(time.time())
    claims = dict(
        iss="https://tenant.auth0.test/",
        aud="https://mcp.example.test/mcp",
        sub="auth0|owner",
        iat=now - 1,
        exp=now + 300,
        scope="commander:use",
        azp="chatgpt-client",
    )
    claims.update(overrides)
    for name in remove:
        claims.pop(name, None)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def verifier(key, **overrides) -> OAuthTokenVerifier:
    def handler(request):
        assert str(request.url) == "https://tenant.auth0.test/.well-known/jwks.json"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"keys": [jwk(key)]})

    return OAuthTokenVerifier(oauth_settings(**overrides), transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_accepts_only_owner_and_returns_principal(signing_key):
    result = await verifier(signing_key).verify_token(access_token(signing_key))
    assert result is not None
    assert result.subject == "auth0|owner"
    assert result.client_id == "chatgpt-client"
    assert result.resource == "https://mcp.example.test/mcp"
    assert result.claims == {"iss": "https://tenant.auth0.test/"}
    assert result.expires_at > time.time()


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://other.auth0.test/"},
        {"aud": "https://another.example.test/mcp"},
        {"sub": "auth0|other-user"},
        {"sub": "client-id@clients"},
        {"exp": 1},
        {"exp": "9999999999"},
        {"exp": float("inf")},
        {"exp": float("nan")},
        {"iat": int(time.time()) + 300},
        {"nbf": int(time.time()) + 300},
        {"scope": "commander:use-other"},
        {"scope": ["commander:use"]},
        {"azp": None},
    ],
)
@pytest.mark.asyncio
async def test_rejects_invalid_claims(signing_key, claims):
    assert await verifier(signing_key).verify_token(access_token(signing_key, **claims)) is None


@pytest.mark.parametrize("claim", ["iss", "aud", "sub", "exp", "iat"])
@pytest.mark.asyncio
async def test_rejects_missing_required_claim(signing_key, claim):
    assert (
        await verifier(signing_key).verify_token(access_token(signing_key, remove=[claim])) is None
    )


@pytest.mark.asyncio
async def test_rejects_wrong_signature_static_unsigned_and_hmac_tokens(signing_key):
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    instance = verifier(signing_key)
    tokens = [
        access_token(other_key),
        "legacy-token-not-accepted",
        "not-a-jwt",
        jwt.encode({"sub": "auth0|owner"}, "", algorithm="none"),
        jwt.encode({"sub": "auth0|owner"}, "x" * 32, algorithm="HS256"),
        "x" * 16_385,
    ]
    for token in tokens:
        assert await instance.verify_token(token) is None


@pytest.mark.asyncio
async def test_audience_array_and_client_id_claim_supported(signing_key):
    result = await verifier(signing_key).verify_token(
        access_token(
            signing_key,
            aud=["https://mcp.example.test/mcp", "https://tenant.auth0.test/userinfo"],
            remove=["azp"],
            client_id="chatgpt-client",
        )
    )
    assert result is not None


@pytest.mark.asyncio
async def test_caches_jwks_and_bounds_unknown_key_refresh(signing_key, monkeypatch):
    calls = []
    clock = [100.0]
    keys = [jwk(signing_key)]
    monkeypatch.setattr("remote_mcp_commander.oauth.time.monotonic", lambda: clock[0])

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"keys": keys})

    instance = OAuthTokenVerifier(oauth_settings(), transport=httpx.MockTransport(handler))
    token = access_token(signing_key)
    results = await asyncio.gather(*(instance.verify_token(token) for _ in range(10)))
    assert all(result is not None for result in results)
    assert len(calls) == 1
    for kid in ["unknown-1", "unknown-2"]:
        assert await instance.verify_token(access_token(signing_key, kid=kid)) is None
    assert len(calls) == 1
    keys.append(jwk(signing_key, "key-2"))
    clock[0] += 31
    assert await instance.verify_token(access_token(signing_key, kid="key-2")) is not None
    assert len(calls) == 2


@pytest.mark.parametrize(
    "document",
    [[], {}, {"keys": []}, {"keys": [42]}, {"keys": [{"kty": "RSA", "kid": "bad"}]}],
)
@pytest.mark.asyncio
async def test_malformed_provider_data_fails_closed(signing_key, document):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=document))
    instance = OAuthTokenVerifier(oauth_settings(), transport=transport)
    assert await instance.verify_token(access_token(signing_key)) is None


@pytest.mark.asyncio
async def test_provider_outage_redirect_and_large_response_fail_closed(signing_key):
    for response in [
        httpx.Response(503),
        httpx.Response(302, headers={"location": "https://evil.test/keys"}),
        httpx.Response(200, content=b"x" * 1_048_577),
    ]:
        calls = []

        def handler(request, calls=calls, response=response):
            calls.append(request)
            return response

        instance = OAuthTokenVerifier(oauth_settings(), transport=httpx.MockTransport(handler))
        assert await instance.verify_token(access_token(signing_key)) is None
        assert await instance.verify_token(access_token(signing_key)) is None
        assert len(calls) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"mcp_oauth_subject": ""},
        {"mcp_oauth_subject": " auth0|owner "},
        {"mcp_issuer_url": "http://127.0.0.1"},
        {"mcp_issuer_url": "https://user:secret@tenant.auth0.test/"},
        {"mcp_issuer_url": "https://tenant.auth0.test/?secret=x"},
        {"mcp_resource_url": "https://mcp.example.test/wrong"},
        {"mcp_resource_url": "https://mcp.example.test/mcp#fragment"},
        {"mcp_scope": ""},
        {"mcp_scope": "commander:use another"},
    ],
)
def test_rejects_unsafe_oauth_configuration(overrides):
    with pytest.raises(ValueError):
        oauth_settings(**overrides).validate_mcp_http_security()


def test_oauth_configuration_does_not_need_static_token():
    oauth_settings(mcp_token="").validate_mcp_http_security()


@pytest.mark.asyncio
async def test_untrusted_jwt_key_url_is_never_followed(signing_key):
    claims = jwt.decode(access_token(signing_key), options={"verify_signature": False})
    forged_url = jwt.encode(
        claims,
        signing_key,
        algorithm="RS256",
        headers={"kid": "key-1", "jku": "https://attacker.test/keys"},
    )
    assert await verifier(signing_key).verify_token(forged_url) is not None


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"kid": "key-1", "crit": ["custom"]},
        {"kid": "key-1", "b64": False},
    ],
)
@pytest.mark.asyncio
async def test_rejects_missing_kid_and_unsupported_critical_headers(signing_key, headers):
    claims = jwt.decode(access_token(signing_key), options={"verify_signature": False})
    token = jwt.encode(claims, signing_key, algorithm="RS256", headers=headers)
    assert await verifier(signing_key).verify_token(token) is None


@pytest.mark.parametrize("key_record", ["duplicate", "private", "weak", "wrong-use", "bad-ops"])
@pytest.mark.asyncio
async def test_rejects_unusable_provider_keys(signing_key, key_record):
    record = jwk(signing_key)
    records = [record]
    if key_record == "duplicate":
        records.append(record.copy())
    elif key_record == "private":
        record["d"] = "private-key-material"
    elif key_record == "weak":
        weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        records = [jwk(weak)]
    elif key_record == "wrong-use":
        record["use"] = "enc"
    else:
        record["key_ops"] = "verify"
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"keys": records}))
    instance = OAuthTokenVerifier(oauth_settings(), transport=transport)
    assert await instance.verify_token(access_token(signing_key)) is None


@pytest.mark.asyncio
async def test_expired_key_cache_is_not_used_during_provider_outage(signing_key, monkeypatch):
    clock = [100.0]
    available = [True]
    monkeypatch.setattr("remote_mcp_commander.oauth.time.monotonic", lambda: clock[0])

    def handler(request):
        return (
            httpx.Response(200, json={"keys": [jwk(signing_key)]})
            if available[0]
            else httpx.Response(503)
        )

    instance = OAuthTokenVerifier(oauth_settings(), transport=httpx.MockTransport(handler))
    token = access_token(signing_key)
    assert await instance.verify_token(token) is not None
    available[0] = False
    clock[0] += 301
    assert await instance.verify_token(token) is None

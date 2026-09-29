"""Single-user resource-server authentication for Auth0 RS256 access tokens.

The identity provider owns login, PKCE, consent and refresh. Never follow URLs
from an untrusted JWT, and never accept the legacy static token in OAuth mode.
"""

from __future__ import annotations

import asyncio
import json
import math
import time

import httpx
import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier

from remote_mcp_commander.config import Settings


class OAuthTokenVerifier(TokenVerifier):
    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self._transport = transport
        self._keys: dict[str, jwt.PyJWK] = {}
        self._expires_at = 0.0
        self._retry_after = 0.0
        self._lock = asyncio.Lock()

    async def _key(self, kid: str) -> jwt.PyJWK | None:
        async with self._lock:
            now = time.monotonic()
            if now < self._expires_at and kid in self._keys:
                return self._keys[kid]
            # Unknown kids and provider failures cannot cause an unbounded fetch storm.
            if now < self._retry_after:
                return None
            self._retry_after = now + 30
            url = self.settings.mcp_issuer_url.rstrip("/") + "/.well-known/jwks.json"
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self.settings.mcp_oauth_timeout_s,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 1_048_576:
                            raise ValueError("JWKS response exceeds limit")
            document = json.loads(body)
            if not isinstance(document, dict):
                raise ValueError("invalid JWKS document")
            records = document.get("keys")
            if not isinstance(records, list) or not 1 <= len(records) <= 64:
                raise ValueError("invalid JWKS key count")
            keys: dict[str, jwt.PyJWK] = {}
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError("invalid JWK")
                operations = record.get("key_ops", ["verify"])
                if (
                    record.get("kty") != "RSA"
                    or record.get("use", "sig") != "sig"
                    or record.get("alg", "RS256") != "RS256"
                    or "d" in record
                    or not isinstance(operations, list)
                    or "verify" not in operations
                ):
                    continue
                key_id = record.get("kid")
                if not isinstance(key_id, str) or not key_id or key_id in keys:
                    raise ValueError("missing or duplicate signing key ID")
                key = jwt.PyJWK.from_dict(record, algorithm="RS256")
                if key.key.key_size < 2048:
                    raise ValueError("RSA signing key is too small")
                keys[key_id] = key
            self._keys = keys
            self._expires_at = time.monotonic() + self.settings.mcp_oauth_jwks_cache_s
            return keys.get(kid)

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or len(token) > 16_384:
            return None
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if (
                header.get("alg") != "RS256"
                or header.get("crit") is not None
                or header.get("b64", True) is not True
                or not isinstance(kid, str)
                or not 1 <= len(kid) <= 256
            ):
                return None
            key = await self._key(kid)
            if key is None:
                return None
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self.settings.mcp_issuer_url,
                audience=self.settings.mcp_resource_url,
                options={"require": ["iss", "aud", "exp", "iat", "sub"]},
            )
            for name in ("exp", "iat", "nbf"):
                value = claims.get(name)
                if name == "nbf" and value is None:
                    continue
                if type(value) not in (int, float) or not math.isfinite(value):
                    return None
            if claims["sub"] != self.settings.mcp_oauth_subject:
                return None
            scope = claims.get("scope", "")
            if not isinstance(scope, str):
                return None
            scopes = scope.split()
            if self.settings.mcp_scope not in scopes:
                return None
            client_id = claims.get("azp") or claims.get("client_id")
            if not isinstance(client_id, str) or not client_id:
                return None
            return AccessToken(
                token=token,
                client_id=client_id,
                scopes=scopes,
                expires_at=int(claims["exp"]),
                resource=self.settings.mcp_resource_url,
                subject=claims["sub"],
                claims={"iss": claims["iss"]},
            )
        except (jwt.PyJWTError, httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError):
            # Fail closed, without logging tokens, claims or provider response bodies.
            return None

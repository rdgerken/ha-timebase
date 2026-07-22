"""Timebase Pulse (OIDC identity provider) authentication.

Pulse secures the Historian REST API with OAuth 2.0. Machine clients use the
client-credentials grant; a new Pulse instance pre-creates clients (Historian,
Collector, Explorer) whose tokens carry an audience ("aud") claim the
Historian validates.

The token endpoint is resolved via standard OIDC discovery
(/.well-known/openid-configuration) against the configured Pulse base URL,
with conventional fallbacks (/connect/token, /oauth/token) if discovery is
unavailable. Tokens are cached and refreshed shortly before expiry.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from .api import REQUEST_TIMEOUT, TimebaseConnectionError, TimebaseError

_LOGGER = logging.getLogger(__name__)

DISCOVERY_PATH = "/.well-known/openid-configuration"
FALLBACK_TOKEN_PATHS = ("/connect/token", "/oauth/token")
DEFAULT_AUDIENCE = "Historian"
TOKEN_REFRESH_MARGIN_SECONDS = 60
DEFAULT_TOKEN_LIFETIME_SECONDS = 3600


class PulseAuthError(TimebaseError):
    """Pulse rejected the client credentials or token request."""


class PulseAuth:
    """Client-credentials token source for the Historian REST API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        pulse_url: str,
        client_id: str,
        client_secret: str,
        audience: str = DEFAULT_AUDIENCE,
        verify_ssl: bool = True,
    ) -> None:
        self._session = session
        self._base = pulse_url.rstrip("/")
        self._ssl_kwargs: dict[str, Any] = (
            {} if verify_ssl else {"ssl": False}
        )
        self._client_id = client_id
        self._client_secret = client_secret
        self._audience = audience
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._token_endpoint: str | None = None
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        """Drop the cached token (e.g. after a 401 from the historian)."""
        self._token = None

    async def async_get_token(self) -> str:
        """Return a valid access token, fetching/refreshing as needed."""
        async with self._lock:
            if (
                self._token
                and time.monotonic()
                < self._expires_at - TOKEN_REFRESH_MARGIN_SECONDS
            ):
                return self._token
            await self._async_fetch_token()
            assert self._token is not None
            return self._token

    async def _async_discover_token_endpoint(self) -> str | None:
        """Resolve the token endpoint via OIDC discovery."""
        url = f"{self._base}{DISCOVERY_PATH}"
        try:
            async with self._session.get(
                url, timeout=REQUEST_TIMEOUT, **self._ssl_kwargs
            ) as resp:
                if resp.status != 200:
                    return None
                doc: dict[str, Any] = await resp.json(content_type=None)
                endpoint = doc.get("token_endpoint")
                if endpoint:
                    _LOGGER.debug("Pulse token endpoint discovered: %s", endpoint)
                return endpoint
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None

    async def _async_fetch_token(self) -> None:
        """Fetch a fresh token, resolving the endpoint on first use."""
        if self._token_endpoint is None:
            self._token_endpoint = await self._async_discover_token_endpoint()

        candidates = (
            [self._token_endpoint]
            if self._token_endpoint
            else [f"{self._base}{path}" for path in FALLBACK_TOKEN_PATHS]
        )

        form = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        if self._audience:
            # Ignored by servers that scope audience via client config.
            form["audience"] = self._audience

        last_error: str = "no token endpoint responded"
        for endpoint in candidates:
            try:
                async with self._session.post(
                    endpoint, data=form, timeout=REQUEST_TIMEOUT, **self._ssl_kwargs
                ) as resp:
                    body = await resp.json(content_type=None)
                    if resp.status != 200:
                        last_error = (
                            f"{endpoint} -> {resp.status}: "
                            f"{body.get('error', body) if isinstance(body, dict) else body}"
                        )
                        continue
                    token = body.get("access_token")
                    if not token:
                        last_error = f"{endpoint} -> 200 without access_token"
                        continue
                    self._token = token
                    self._token_endpoint = endpoint
                    self._expires_at = time.monotonic() + float(
                        body.get("expires_in", DEFAULT_TOKEN_LIFETIME_SECONDS)
                    )
                    return
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise TimebaseConnectionError(
                    f"Cannot reach Pulse at {endpoint}: {err}"
                ) from err
            except ValueError as err:  # non-JSON body
                last_error = f"{endpoint} -> invalid response: {err}"

        raise PulseAuthError(f"Pulse token request failed: {last_error}")

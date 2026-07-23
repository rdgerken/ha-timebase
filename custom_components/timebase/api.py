"""Async client for the Timebase Historian public REST API.

API reference: http://<historian>:4511/api/help (Swagger)
Docs: https://docs.timebase.flow-software.com/knowledge-base/timebase-historian-public-rest-api

Payload conventions (Timebase uses terse keys; verified against a live
historian):
  dataset: {"n": name, "pa": purge_age_days, "ps": purge_size_gb, ...}
  tag:     {"n": name, "d": description, "f": format, "u": units, "t": data_type}
           "u" MUST be an enumeration dict, e.g. {"1": "kWh"} for a unit or
           {"0": "Off", "1": "On"} for value labels — a plain string is a 400.
  TVQ:     {"t": iso8601_timestamp, "v": value, "q": quality}
  GET tags returns {"User": [...], "System": [...]}; GET data returns
  {"s": start, "e": end, "tl": [{"t": <tagmeta>, "d": [tvq, ...]}, ...]}.

IMPORTANT (empirically verified): Timebase SILENTLY drops any TVQ whose
timestamp is older than the tag's newest stored point — even within the
current hour block — while still returning HTTP 200. The only evidence is a
"late data rejected" warning in the historian's log. Callers must write in
strict chronological order per tag; backfill works into fresh tags only.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

import aiohttp

from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from .auth import PulseAuth

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


class TimebaseError(Exception):
    """Base error for Timebase API failures."""


class TimebaseConnectionError(TimebaseError):
    """The historian could not be reached."""


class TimebaseApiError(TimebaseError):
    """The historian returned an error response."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Timebase API error {status}: {message}")
        self.status = status


def unit_from_meta(meta: dict[str, Any]) -> str | None:
    """Extract a unit string from Timebase tag metadata.

    Units are single-entry enumeration dicts, e.g. {"1": "MB"}; multi-entry
    dicts are value enumerations (state labels), not units.
    """
    u = meta.get("u")
    if isinstance(u, str):
        return u or None
    if isinstance(u, dict) and len(u) == 1:
        value = next(iter(u.values()))
        return str(value) if value else None
    return None


def iso_z(when: datetime) -> str:
    """Format a datetime as ISO 8601 UTC with a Z suffix (Timebase style)."""
    return (
        dt_util.as_utc(when)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class TimebaseClient:
    """Thin async wrapper over the Timebase Historian REST API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        auth: PulseAuth | None = None,
        use_ssl: bool = False,
        verify_ssl: bool = True,
    ) -> None:
        self._session = session
        scheme = "https" if use_ssl else "http"
        self.base_url = f"{scheme}://{host}:{port}/api"
        self._auth = auth
        # aiohttp: ssl=False disables certificate verification (self-signed
        # certs — Timebase generates one and redirects HTTP -> HTTPS).
        self._ssl_kwargs: dict[str, Any] = (
            {} if verify_ssl else {"ssl": False}
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: list[tuple[str, str]] | dict[str, str] | None = None,
        json: Any | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        # Two attempts: on 401 the Pulse token is invalidated and re-fetched.
        for attempt in (1, 2):
            headers: dict[str, str] = {}
            if self._auth is not None:
                headers["Authorization"] = (
                    f"Bearer {await self._auth.async_get_token()}"
                )
            try:
                async with self._session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                    **self._ssl_kwargs,
                ) as resp:
                    if resp.status == 401 and self._auth is not None and attempt == 1:
                        _LOGGER.debug("401 from %s, refreshing Pulse token", url)
                        self._auth.invalidate()
                        continue
                    if resp.status >= 400:
                        text = await resp.text()
                        raise TimebaseApiError(resp.status, text[:500])
                    if resp.content_type == "application/json":
                        return await resp.json()
                    return await resp.text()
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise TimebaseConnectionError(
                    f"Cannot reach Timebase at {url}: {err}"
                ) from err
        raise TimebaseApiError(401, "Unauthorized after token refresh")

    # --- Datasets -----------------------------------------------------------

    async def async_get_datasets(self) -> list[dict[str, Any]]:
        """Return all datasets. Also serves as the connectivity check."""
        data = await self._request("GET", "/datasets")
        return data if isinstance(data, list) else []

    async def async_ensure_dataset(
        self, name: str, retention_days: int
    ) -> None:
        """Create the dataset if it does not exist yet."""
        datasets = await self.async_get_datasets()
        if any(ds.get("n") == name for ds in datasets):
            return
        _LOGGER.info("Creating Timebase dataset %s", name)
        await self._request(
            "POST",
            "/datasets",
            json={"n": name, "pa": retention_days, "ps": 0},
        )

    # --- Tags ---------------------------------------------------------------

    async def async_get_tags(
        self, dataset: str, contains: str | None = None
    ) -> list[dict[str, Any]]:
        params = {"contains": contains} if contains else None
        data = await self._request(
            "GET", f"/datasets/{dataset}/tags", params=params
        )
        if isinstance(data, dict):
            # Live API groups tags: {"User": [...], "System": [...]}.
            tags: list[dict[str, Any]] = []
            for group in data.values():
                if isinstance(group, list):
                    tags.extend(t for t in group if isinstance(t, dict))
            return tags
        return data if isinstance(data, list) else []

    async def async_upsert_tags(
        self, dataset: str, tags: list[dict[str, Any]]
    ) -> None:
        """Create or update tags. Each item: {"n": name, "d": desc, "u": units}."""
        await self._request("POST", f"/datasets/{dataset}/tags", json=tags)

    # --- Data ---------------------------------------------------------------

    async def async_write(
        self, dataset: str, payload: dict[str, list[dict[str, Any]]]
    ) -> None:
        """Write TVQs to multiple tags: {"tag.name": [{"t","v","q"}, ...]}."""
        await self._request("POST", f"/datasets/{dataset}/data", json=payload)

    async def async_write_status(
        self, dataset: str, tvq: dict[str, Any]
    ) -> None:
        """Post a dataset status indicator (comms loss, collector shutdown).

        Takes a SINGLE TVQ object — the docs show an array, but the live API
        400s on one and accepts {"t", "v", "q"}.
        """
        await self._request("POST", f"/datasets/{dataset}/status", json=tvq)

    async def async_read(
        self,
        dataset: str,
        tagnames: list[str],
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Read raw TVQs for tags; no start => latest point per tag.

        Returns {tagname: [{"t","v","q"}, ...]} normalized from the API shape.
        """
        params: list[tuple[str, str]] = [("tagname", t) for t in tagnames]
        if start is not None:
            params.append(("start", iso_z(start)))
        if end is not None:
            params.append(("end", iso_z(end)))
        data = await self._request(
            "GET", f"/datasets/{dataset}/data", params=params
        )
        return self._normalize_read(data)

    @staticmethod
    def _normalize_read(data: Any) -> dict[str, list[dict[str, Any]]]:
        """Normalize data read responses to {name: tvqs}.

        Live API shape: {"s": start, "e": end, "tl": [{"t": meta, "d": [tvq]}]}.
        Older/single-tag shapes without the "tl" wrapper are also handled.
        Empty tags yield placeholder TVQs with no "v" key — callers skip those.
        """
        result: dict[str, list[dict[str, Any]]] = {}
        if isinstance(data, dict) and isinstance(data.get("tl"), list):
            items: list[Any] = data["tl"]
        elif isinstance(data, list):
            items = data
        else:
            items = [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            meta = item.get("t")
            name = meta.get("n") if isinstance(meta, dict) else item.get("n")
            if name:
                result[str(name)] = item.get("d") or []
        return result

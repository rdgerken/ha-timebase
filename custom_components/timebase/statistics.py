"""Import Timebase tags into Home Assistant long-term statistics.

Periodically reads raw TVQs for configured tags, aggregates them into hourly
buckets, and inserts them via async_add_external_statistics with
`timebase:<slug>` statistic IDs. That surfaces historian data in HA's native
statistics UI (statistics-graph cards, energy dashboard) without touching the
recorder schema.

Two tag kinds:
- Measurement tags -> hourly mean/min/max (temperatures, pressures, rates).
- Counter tags     -> hourly state + cumulative sum (energy, water, gas
  meters). Deltas are computed between hourly readings, meter resets are
  detected (reading drops -> delta = new reading), and the running sum
  resumes from the last stored statistic so re-imports never double-count.

Intended for tags collected by OTHER Timebase collectors (OPC UA, MQTT,
Sparkplug plant/equipment data). Do NOT import tags this integration exported
from HA — the source entities already have native statistics.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .api import TimebaseClient, TimebaseError, unit_from_meta
from .const import (
    DEFAULT_IMPORT_INTERVAL_MINUTES,
    DOMAIN,
    IMPORT_MAX_LOOKBACK_HOURS,
)

_LOGGER = logging.getLogger(__name__)

# StatisticMeanType landed in HA 2025.4 (has_mean deprecated after).
try:  # pragma: no cover - version shim
    from homeassistant.components.recorder.models import StatisticMeanType

    _HAS_MEAN_TYPE = True
except ImportError:  # pragma: no cover
    StatisticMeanType = None  # type: ignore[assignment]
    _HAS_MEAN_TYPE = False


def tag_to_statistic_id(tag: str) -> str:
    """Convert a Timebase tag name to a valid external statistic ID."""
    slug = re.sub(r"[^a-z0-9_]", "_", tag.lower())
    return f"{DOMAIN}:{slug}"


def _floor_hour(when: datetime) -> datetime:
    return when.replace(minute=0, second=0, microsecond=0)


class TimebaseStatisticsImporter:
    """Pulls hourly aggregates from Timebase into HA long-term statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: TimebaseClient,
        dataset: str,
        measurement_tags: list[str],
        counter_tags: list[str],
    ) -> None:
        self._hass = hass
        self._client = client
        self._dataset = dataset
        # A tag listed in both is treated as a counter.
        self._counter_tags = list(dict.fromkeys(counter_tags))
        self._measurement_tags = [
            t for t in dict.fromkeys(measurement_tags)
            if t not in self._counter_tags
        ]
        self._units: dict[str, str | None] = {}
        self._unsub = None

        # Diagnostics
        self.last_run: datetime | None = None
        self.last_error: str | None = None
        self.hours_imported = 0

    def async_start(self) -> None:
        self._unsub = async_track_time_interval(
            self._hass,
            self._async_import_tick,
            timedelta(minutes=DEFAULT_IMPORT_INTERVAL_MINUTES),
        )
        # Kick off an initial import shortly after startup.
        self._hass.async_create_task(self.async_import())

    def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    async def _async_import_tick(self, _now: Any = None) -> None:
        await self.async_import()

    async def async_import(self) -> None:
        """Import completed hours for every configured tag."""
        if not self._measurement_tags and not self._counter_tags:
            return
        self.last_run = dt_util.utcnow()
        try:
            await self._async_refresh_units()
            for tag in self._measurement_tags:
                await self._async_import_measurement(tag)
            for tag in self._counter_tags:
                await self._async_import_counter(tag)
            self.last_error = None
        except TimebaseError as err:
            self.last_error = str(err)
            _LOGGER.warning("Timebase statistics import failed: %s", err)

    # --- Shared helpers -----------------------------------------------------

    async def _async_refresh_units(self) -> None:
        """Cache tag units (`u`) from Timebase tag metadata."""
        if self._units:
            return
        tags_meta = await self._client.async_get_tags(self._dataset)
        by_name = {t.get("n"): t for t in tags_meta if isinstance(t, dict)}
        for tag in self._measurement_tags + self._counter_tags:
            self._units[tag] = unit_from_meta(by_name.get(tag) or {})

    async def _async_read_points(
        self, tag: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, float]]:
        """Read raw TVQs for a tag as time-sorted (datetime, float) pairs."""
        tvqs = (
            await self._client.async_read(
                self._dataset, [tag], start=start, end=end
            )
        ).get(tag, [])
        points: list[tuple[datetime, float]] = []
        for tvq in tvqs:
            raw_t, raw_v = tvq.get("t"), tvq.get("v")
            if raw_t is None or raw_v is None:
                continue
            try:
                value = float(raw_v)
            except (TypeError, ValueError):
                continue
            when = dt_util.parse_datetime(str(raw_t))
            if when is None:
                continue
            when = dt_util.as_utc(when)
            if start <= when < end:
                points.append((when, value))
        points.sort(key=lambda p: p[0])
        return points

    async def _async_get_last_stat(
        self, statistic_id: str, types: set[str]
    ) -> dict[str, Any] | None:
        """Return the newest stored statistics row for an ID, if any."""
        last = await get_instance(self._hass).async_add_executor_job(
            get_last_statistics, self._hass, 1, statistic_id, False, types
        )
        rows = last.get(statistic_id) or []
        return rows[0] if rows else None

    @staticmethod
    def _row_start(row: dict[str, Any]) -> datetime | None:
        raw = row.get("start")
        if raw is None:
            return None
        if isinstance(raw, datetime):
            return dt_util.as_utc(raw)
        return dt_util.utc_from_timestamp(float(raw))  # unix ts on modern HA

    def _metadata(self, tag: str, *, is_counter: bool) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "source": DOMAIN,
            "statistic_id": tag_to_statistic_id(tag),
            "name": f"Timebase {tag}",
            "unit_of_measurement": self._units.get(tag),
            "unit_class": None,  # no unit conversion (required key on newer HA)
            "has_sum": is_counter,
        }
        if _HAS_MEAN_TYPE:
            metadata["mean_type"] = (
                StatisticMeanType.NONE if is_counter
                else StatisticMeanType.ARITHMETIC
            )
        else:
            metadata["has_mean"] = not is_counter
        return metadata

    def _resume_window(
        self, last_row: dict[str, Any] | None
    ) -> tuple[datetime, datetime]:
        """Compute [start, end) of hours still to import."""
        window_end = _floor_hour(dt_util.utcnow())
        if last_row is not None and (last_start := self._row_start(last_row)):
            start = last_start + timedelta(hours=1)
        else:
            start = window_end - timedelta(hours=IMPORT_MAX_LOOKBACK_HOURS)
        return start, window_end

    # --- Measurement tags: hourly mean/min/max ------------------------------

    async def _async_import_measurement(self, tag: str) -> None:
        statistic_id = tag_to_statistic_id(tag)
        last_row = await self._async_get_last_stat(statistic_id, {"start"})
        start, window_end = self._resume_window(last_row)
        if start >= window_end:
            return
        points = await self._async_read_points(tag, start, window_end)
        if not points:
            return

        buckets: dict[datetime, list[float]] = defaultdict(list)
        for when, value in points:
            buckets[_floor_hour(when)].append(value)

        stats = [
            {
                "start": bucket_start,
                "mean": sum(vals) / len(vals),
                "min": min(vals),
                "max": max(vals),
            }
            for bucket_start, vals in sorted(buckets.items())
        ]
        async_add_external_statistics(
            self._hass, self._metadata(tag, is_counter=False), stats
        )
        self.hours_imported += len(stats)
        _LOGGER.debug(
            "Imported %d measurement buckets for %s", len(stats), statistic_id
        )

    # --- Counter tags: hourly state + cumulative sum ------------------------

    async def _async_import_counter(self, tag: str) -> None:
        statistic_id = tag_to_statistic_id(tag)
        last_row = await self._async_get_last_stat(
            statistic_id, {"start", "state", "sum"}
        )
        start, window_end = self._resume_window(last_row)
        if start >= window_end:
            return
        points = await self._async_read_points(tag, start, window_end)
        if not points:
            return

        # Last reading per hour is the meter state for that hour.
        readings: dict[datetime, float] = {}
        for when, value in points:
            readings[_floor_hour(when)] = value

        prev_state: float | None = None
        prev_sum = 0.0
        if last_row is not None:
            prev_state = (
                float(last_row["state"])
                if last_row.get("state") is not None
                else None
            )
            prev_sum = (
                float(last_row["sum"])
                if last_row.get("sum") is not None
                else 0.0
            )

        stats: list[dict[str, Any]] = []
        for bucket_start in sorted(readings):
            state = readings[bucket_start]
            if prev_state is None:
                delta = 0.0  # first ever import: establish the baseline
            elif state < prev_state:
                delta = state  # meter reset: count from zero
            else:
                delta = state - prev_state
            prev_sum += delta
            prev_state = state
            stats.append(
                {"start": bucket_start, "state": state, "sum": prev_sum}
            )

        async_add_external_statistics(
            self._hass, self._metadata(tag, is_counter=True), stats
        )
        self.hours_imported += len(stats)
        _LOGGER.debug(
            "Imported %d counter buckets for %s", len(stats), statistic_id
        )

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "measurement_tags": self._measurement_tags,
            "counter_tags": self._counter_tags,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "hours_imported": self.hours_imported,
            "last_error": self.last_error,
        }

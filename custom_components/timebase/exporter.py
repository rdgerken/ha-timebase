"""Stream Home Assistant state changes into Timebase as TVQ writes.

Design notes:
- Event-driven (EVENT_STATE_CHANGED), not polling — TVQ timestamps come from
  state.last_updated, so the historian stores *event* time, not receive time.
- Per-tag append-ordered buffer, flushed in batches. Timebase silently ignores
  TVQs older than a tag's newest point, so ordering must be preserved and, on
  buffer overflow, the OLDEST samples are dropped (they would be rejected
  after newer ones land anyway).
- Store-and-forward: on write failure the batch is re-queued at the front of
  the buffer and retried on the next flush tick.
- Tags are auto-provisioned on first sight with friendly name + unit metadata.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.const import (
    ATTR_FRIENDLY_NAME,
    ATTR_UNIT_OF_MEASUREMENT,
    EVENT_HOMEASSISTANT_STOP,
    EVENT_STATE_CHANGED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entityfilter import EntityFilter
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .api import TimebaseClient, TimebaseError, iso_z
from .const import (
    BINARY_STATE_MAP,
    DEFAULT_FLUSH_INTERVAL_SECONDS,
    MAX_BUFFERED_TVQS,
    QUALITY_COLLECTOR_SHUTDOWN,
    QUALITY_COMMS_LOST,
    QUALITY_GOOD,
)

_LOGGER = logging.getLogger(__name__)


class TimebaseExporter:
    """Buffers state changes and batch-writes them to the historian."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: TimebaseClient,
        dataset: str,
        tag_prefix: str,
        entity_filter: EntityFilter,
        export_string_states: bool = False,
    ) -> None:
        self._hass = hass
        self._client = client
        self._dataset = dataset
        self._prefix = tag_prefix
        self._filter = entity_filter
        self._export_strings = export_string_states

        self._buffer: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._buffered_count = 0
        self._tag_meta: dict[str, dict[str, Any]] = {}
        self._last_value: dict[str, float | str] = {}
        self._provisioned: set[str] = set()
        self._unsubs: list[Callable[[], None]] = []
        self._flushing = False

        # Diagnostics counters
        self.samples_sent = 0
        self.samples_dropped = 0
        self.last_error: str | None = None

    # --- Lifecycle ----------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Subscribe to state changes and start the flush loop."""
        self._unsubs.append(
            self._hass.bus.async_listen(EVENT_STATE_CHANGED, self._handle_event)
        )
        self._unsubs.append(
            async_track_time_interval(
                self._hass,
                self._async_flush_tick,
                timedelta(seconds=DEFAULT_FLUSH_INTERVAL_SECONDS),
            )
        )
        self._unsubs.append(
            self._hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, self._async_final_flush
            )
        )

    async def async_stop(self) -> None:
        """Unsubscribe and attempt a final flush."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self.async_flush()

    # --- Event handling -----------------------------------------------------

    @callback
    def _handle_event(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if new_state is None:
            return  # entity removed
        entity_id = new_state.entity_id
        if not self._filter(entity_id):
            return
        # Attribute-only churn: same state value → no new historian point.
        if old_state is not None and old_state.state == new_state.state:
            return

        tag = f"{self._prefix}.{entity_id}"

        if new_state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, ""):
            # Industrial convention: hold the last known value and flag the
            # sample bad-quality (24 = source comms lost) so trends show the
            # gap explicitly instead of silently interpolating across it.
            last = self._last_value.get(tag)
            if last is None:
                return  # nothing to hold (entity unavailable since startup)
            if tag not in self._tag_meta:
                self._tag_meta[tag] = {"n": tag, "d": entity_id}
            self._buffer[tag].append(
                {
                    "t": iso_z(new_state.last_updated),
                    "v": last,
                    "q": QUALITY_COMMS_LOST,
                }
            )
            self._buffered_count += 1
            self._enforce_bound()
            return

        value = self._convert_state(new_state.state)
        if value is None:
            return

        meta: dict[str, Any] = {
            "n": tag,
            "d": new_state.attributes.get(ATTR_FRIENDLY_NAME, entity_id),
        }
        if unit := new_state.attributes.get(ATTR_UNIT_OF_MEASUREMENT):
            meta["u"] = {"1": str(unit)}  # Timebase units are enum dicts
        self._tag_meta[tag] = meta
        self._last_value[tag] = value
        self._buffer[tag].append(
            {
                "t": iso_z(new_state.last_updated),
                "v": value,
                "q": QUALITY_GOOD,
            }
        )
        self._buffered_count += 1
        self._enforce_bound()

    def _convert_state(self, state: str) -> float | str | None:
        """Map an HA state string to a TVQ value, or None to skip."""
        mapped = BINARY_STATE_MAP.get(state.lower())
        if mapped is not None:
            return mapped
        try:
            return float(state)
        except ValueError:
            return state if self._export_strings else None

    def _enforce_bound(self) -> None:
        """Drop oldest samples when the store-and-forward buffer overflows."""
        while self._buffered_count > MAX_BUFFERED_TVQS:
            for tag, tvqs in self._buffer.items():
                if tvqs:
                    tvqs.pop(0)
                    self._buffered_count -= 1
                    self.samples_dropped += 1
                    break
            else:
                break

    # --- Flushing -----------------------------------------------------------

    async def _async_flush_tick(self, _now: Any = None) -> None:
        await self.async_flush()

    async def _async_final_flush(self, _event: Event) -> None:
        """On HA shutdown: flush, then mark the dataset as collector-stopped."""
        await self.async_flush()
        try:
            await self._client.async_write_status(
                self._dataset,
                {
                    "t": iso_z(dt_util.utcnow()),
                    "v": 0,
                    "q": QUALITY_COLLECTOR_SHUTDOWN,
                },
            )
        except TimebaseError as err:
            _LOGGER.debug("Could not post shutdown status: %s", err)

    async def async_flush(self) -> None:
        """Write buffered TVQs to Timebase; requeue at the front on failure."""
        if self._flushing or not self._buffered_count:
            return
        self._flushing = True
        payload = {tag: tvqs for tag, tvqs in self._buffer.items() if tvqs}
        count = self._buffered_count
        self._buffer = defaultdict(list)
        self._buffered_count = 0
        try:
            await self._async_provision_tags(set(payload))
            await self._client.async_write(self._dataset, payload)
            self.samples_sent += count
            self.last_error = None
        except Exception as err:  # noqa: BLE001 — keep the loop alive
            self.last_error = str(err)
            _LOGGER.warning(
                "Timebase flush of %d samples failed, will retry: %s",
                count,
                err,
            )
            # Requeue in front of anything buffered while we were flushing.
            for tag, tvqs in payload.items():
                self._buffer[tag] = tvqs + self._buffer[tag]
            self._buffered_count += count
            self._enforce_bound()
        finally:
            self._flushing = False

    async def _async_provision_tags(self, tags: set[str]) -> None:
        """Upsert tag metadata for tags we have not provisioned yet."""
        new = tags - self._provisioned
        if not new:
            return
        metas = [self._tag_meta[tag] for tag in sorted(new) if tag in self._tag_meta]
        if metas:
            await self._client.async_upsert_tags(self._dataset, metas)
        self._provisioned |= new

    # --- Diagnostics --------------------------------------------------------

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "buffered": self._buffered_count,
            "sent": self.samples_sent,
            "dropped": self.samples_dropped,
            "provisioned_tags": len(self._provisioned),
            "last_error": self.last_error,
        }

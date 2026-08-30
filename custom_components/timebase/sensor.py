"""Live sensor entities for Timebase tags (latest value polling)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import (
    TimebaseAuthError,
    TimebaseClient,
    TimebaseError,
    unit_from_meta,
)
from .auth import PulseAuthError
from .const import (
    CONF_LIVE_TAGS,
    DEFAULT_LIVE_SCAN_INTERVAL_SECONDS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class TimebaseLiveCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Polls the latest TVQ for all live tags in one batched read."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry,  # TimebaseConfigEntry
        client: TimebaseClient,
        dataset: str,
        tags: list[str],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_live",
            update_interval=timedelta(seconds=DEFAULT_LIVE_SCAN_INTERVAL_SECONDS),
        )
        self.client = client
        self.dataset = dataset
        self.tags = tags
        self.units: dict[str, str | None] = {}

    async def _async_setup(self) -> None:
        """Fetch tag metadata (units) once."""
        try:
            metas = await self.client.async_get_tags(self.dataset)
        except (PulseAuthError, TimebaseAuthError) as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except TimebaseError as err:
            raise UpdateFailed(f"Cannot read tag metadata: {err}") from err
        by_name = {m.get("n"): m for m in metas if isinstance(m, dict)}
        for tag in self.tags:
            self.units[tag] = unit_from_meta(by_name.get(tag) or {})

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            # No start time => Timebase returns the latest point per tag.
            data = await self.client.async_read(self.dataset, self.tags)
        except (PulseAuthError, TimebaseAuthError) as err:
            # Rotated/revoked Pulse credentials: trigger a reauth flow
            # instead of retrying forever.
            raise ConfigEntryAuthFailed(str(err)) from err
        except TimebaseError as err:
            raise UpdateFailed(str(err)) from err
        return {
            tag: tvqs[-1] for tag, tvqs in data.items() if tvqs
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry,  # TimebaseConfigEntry
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one sensor per configured live tag."""
    tags: list[str] = entry.options.get(CONF_LIVE_TAGS, [])
    if not tags:
        return
    coordinator = TimebaseLiveCoordinator(
        hass, entry, entry.runtime_data.client, entry.runtime_data.dataset, tags
    )
    entry.runtime_data.coordinator = coordinator
    await coordinator.async_config_entry_first_refresh()
    async_add_entities(
        TimebaseTagSensor(coordinator, entry.entry_id, tag) for tag in tags
    )


class TimebaseTagSensor(CoordinatorEntity[TimebaseLiveCoordinator], SensorEntity):
    """Latest value of a single Timebase tag."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TimebaseLiveCoordinator,
        entry_id: str,
        tag: str,
    ) -> None:
        super().__init__(coordinator)
        self._tag = tag
        self._attr_name = tag
        self._attr_unique_id = f"{entry_id}_{tag}"
        self._attr_native_unit_of_measurement = coordinator.units.get(tag)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Timebase Historian",
            manufacturer="Flow Software",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        return super().available and self._tag in (self.coordinator.data or {})

    @property
    def native_value(self) -> Any:
        tvq = (self.coordinator.data or {}).get(self._tag)
        return tvq.get("v") if tvq else None

    @property
    def state_class(self) -> SensorStateClass | None:
        """Measurement — but only while the tag's value is numeric.

        The API contract allows string TVQ values; a hardcoded MEASUREMENT
        would trip HA's non-numeric-state warning on those tags.
        """
        if isinstance(self.native_value, (int, float)):
            return SensorStateClass.MEASUREMENT
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        tvq = (self.coordinator.data or {}).get(self._tag) or {}
        return {
            "quality": tvq.get("q"),
            "source_timestamp": tvq.get("t"),
            "dataset": self.coordinator.dataset,
            "tag": self._tag,
        }

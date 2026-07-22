"""Timebase Historian integration for Home Assistant.

Two one-way bridges to a Flow Software Timebase historian:
  1. Export — stream HA state changes into Timebase as TVQ writes.
  2. Import — surface Timebase tags as HA long-term statistics and/or
     live sensor entities.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entityfilter import generate_filter
from homeassistant.util import dt as dt_util

from .api import (
    TimebaseClient,
    TimebaseConnectionError,
    TimebaseError,
    iso_z,
)
from .auth import PulseAuth
from .const import (
    ATTR_TAG,
    ATTR_TIMESTAMP,
    ATTR_VALUE,
    CONF_DATASET,
    CONF_EXCLUDE_ENTITY_GLOBS,
    CONF_EXPORT_ENABLED,
    CONF_EXPORT_STRING_STATES,
    CONF_IMPORT_COUNTER_TAGS,
    CONF_IMPORT_TAGS,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_ENTITY_GLOBS,
    CONF_PULSE_CLIENT_ID,
    CONF_PULSE_CLIENT_SECRET,
    CONF_PULSE_URL,
    CONF_RETENTION_DAYS,
    CONF_TAG_PREFIX,
    CONF_USE_SSL,
    CONF_VERIFY_SSL,
    DEFAULT_EXPORT_ENABLED,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_TAG_PREFIX,
    DOMAIN,
    QUALITY_GOOD,
    SERVICE_FLUSH,
    SERVICE_WRITE,
)
from .exporter import TimebaseExporter
from .statistics import TimebaseStatisticsImporter

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


@dataclass
class TimebaseData:
    """Runtime data stored on the config entry."""

    client: TimebaseClient
    dataset: str
    exporter: TimebaseExporter | None = None
    importer: TimebaseStatisticsImporter | None = None
    coordinator: object | None = field(default=None)


TimebaseConfigEntry = ConfigEntry[TimebaseData]

SERVICE_WRITE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_TAG): cv.string,
        vol.Required(ATTR_VALUE): vol.Any(float, int, bool, cv.string),
        vol.Optional(ATTR_TIMESTAMP): cv.datetime,
    }
)


async def async_setup_entry(
    hass: HomeAssistant, entry: TimebaseConfigEntry
) -> bool:
    """Set up a Timebase historian connection from a config entry."""
    session = async_get_clientsession(hass)
    verify_ssl = entry.data.get(CONF_VERIFY_SSL, True)
    auth = None
    if entry.data.get(CONF_PULSE_URL):
        auth = PulseAuth(
            session,
            entry.data[CONF_PULSE_URL],
            entry.data[CONF_PULSE_CLIENT_ID],
            entry.data[CONF_PULSE_CLIENT_SECRET],
            verify_ssl=verify_ssl,
        )
    client = TimebaseClient(
        session,
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        auth=auth,
        use_ssl=entry.data.get(CONF_USE_SSL, False),
        verify_ssl=verify_ssl,
    )
    dataset = entry.data[CONF_DATASET]
    try:
        await client.async_ensure_dataset(
            dataset, entry.data.get(CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS)
        )
    except TimebaseConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err
    except TimebaseError as err:
        raise ConfigEntryNotReady(f"Dataset setup failed: {err}") from err

    data = TimebaseData(client=client, dataset=dataset)
    entry.runtime_data = data
    opts = entry.options

    # --- Export: HA state changes -> Timebase ---
    if opts.get(CONF_EXPORT_ENABLED, DEFAULT_EXPORT_ENABLED):
        entity_filter = generate_filter(
            include_domains=opts.get(CONF_INCLUDE_DOMAINS, []),
            include_entities=[],
            exclude_domains=[],
            exclude_entities=[],
            include_entity_globs=opts.get(CONF_INCLUDE_ENTITY_GLOBS, []),
            exclude_entity_globs=opts.get(CONF_EXCLUDE_ENTITY_GLOBS, []),
        )
        data.exporter = TimebaseExporter(
            hass,
            client,
            dataset,
            opts.get(CONF_TAG_PREFIX, DEFAULT_TAG_PREFIX),
            entity_filter,
            opts.get(CONF_EXPORT_STRING_STATES, False),
        )
        data.exporter.async_start()

    # --- Import: Timebase tags -> HA long-term statistics ---
    import_tags = opts.get(CONF_IMPORT_TAGS, [])
    counter_tags = opts.get(CONF_IMPORT_COUNTER_TAGS, [])
    if import_tags or counter_tags:
        data.importer = TimebaseStatisticsImporter(
            hass, client, dataset, import_tags, counter_tags
        )
        data.importer.async_start()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _async_register_services(hass)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: TimebaseConfigEntry
) -> bool:
    """Unload a config entry, flushing any buffered samples."""
    data = entry.runtime_data
    if data.importer:
        data.importer.async_stop()
    if data.exporter:
        await data.exporter.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(
    hass: HomeAssistant, entry: TimebaseConfigEntry
) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register domain services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_FLUSH):
        return

    def _loaded_entries() -> list[TimebaseConfigEntry]:
        return [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if hasattr(entry, "runtime_data") and entry.runtime_data
        ]

    async def _handle_flush(call: ServiceCall) -> None:
        for entry in _loaded_entries():
            if entry.runtime_data.exporter:
                await entry.runtime_data.exporter.async_flush()

    async def _handle_write(call: ServiceCall) -> None:
        """Write an arbitrary TVQ — lets automations historize computed values."""
        entries = _loaded_entries()
        if not entries:
            raise HomeAssistantError("No Timebase historian is configured")
        entry = entries[0]  # TODO: config_entry_id targeting for multi-historian
        when = call.data.get(ATTR_TIMESTAMP) or dt_util.utcnow()
        value = call.data[ATTR_VALUE]
        if isinstance(value, bool):
            value = 1.0 if value else 0.0
        try:
            await entry.runtime_data.client.async_write(
                entry.runtime_data.dataset,
                {
                    call.data[ATTR_TAG]: [
                        {"t": iso_z(when), "v": value, "q": QUALITY_GOOD}
                    ]
                },
            )
        except TimebaseError as err:
            raise HomeAssistantError(f"Timebase write failed: {err}") from err

    hass.services.async_register(DOMAIN, SERVICE_FLUSH, _handle_flush)
    hass.services.async_register(
        DOMAIN,
        SERVICE_WRITE,
        _handle_write,
        schema=SERVICE_WRITE_SCHEMA,
        supports_response=SupportsResponse.NONE,
    )

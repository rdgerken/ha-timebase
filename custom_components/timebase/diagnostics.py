"""Diagnostics support for the Timebase Historian integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_PULSE_CLIENT_ID, CONF_PULSE_CLIENT_SECRET

TO_REDACT = {CONF_PULSE_CLIENT_ID, CONF_PULSE_CLIENT_SECRET}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = entry.runtime_data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "exporter": data.exporter.stats if data.exporter else None,
        "importer": data.importer.stats if data.importer else None,
    }

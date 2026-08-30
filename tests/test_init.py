"""Entry setup/unload behavior: auth failures and service lifecycle."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.exceptions import ServiceValidationError

from custom_components.timebase.api import TimebaseAuthError
from custom_components.timebase.const import DOMAIN

ENTRY_DATA = {
    "host": "127.0.0.1",
    "port": 4512,
    "dataset": "HomeAssistant",
    "retention_days": 1825,
    "use_ssl": True,
    "verify_ssl": False,
}


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, unique_id="127.0.0.1:4512:HomeAssistant"
    )


async def test_auth_error_on_setup_starts_reauth(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
):
    """A 401/403 at setup must prompt for credentials, not retry forever."""
    entry = _entry()
    entry.add_to_hass(hass)
    with patch("custom_components.timebase.TimebaseClient") as client_cls:
        client_cls.return_value.async_ensure_dataset = AsyncMock(
            side_effect=TimebaseAuthError(401, "token rejected")
        )
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"]["source"] == SOURCE_REAUTH for f in flows)


async def test_services_removed_when_last_entry_unloads(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
):
    entry = _entry()
    entry.add_to_hass(hass)
    with patch("custom_components.timebase.TimebaseClient") as client_cls:
        client_cls.return_value.async_ensure_dataset = AsyncMock(return_value=None)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.services.has_service(DOMAIN, "write")
        assert hass.services.has_service(DOMAIN, "flush")

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert not hass.services.has_service(DOMAIN, "write")
    assert not hass.services.has_service(DOMAIN, "flush")


def _second_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={**ENTRY_DATA, "host": "127.0.0.2"},
        unique_id="127.0.0.2:4512:HomeAssistant",
    )


async def test_write_service_targets_by_config_entry_id(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
):
    entry1, entry2 = _entry(), _second_entry()
    entry1.add_to_hass(hass)
    entry2.add_to_hass(hass)
    clients = [MagicMock(), MagicMock()]
    for c in clients:
        c.async_ensure_dataset = AsyncMock(return_value=None)
        c.async_write = AsyncMock(return_value=None)
    with patch(
        "custom_components.timebase.TimebaseClient", side_effect=clients
    ):
        # Setting up the first entry loads the whole domain, entry2 included.
        assert await hass.config_entries.async_setup(entry1.entry_id)
        await hass.async_block_till_done()
        assert entry2.state is ConfigEntryState.LOADED

        # Ambiguous: two historians, no config_entry_id
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN, "write", {"tag": "x", "value": 1}, blocking=True
            )

        # Targeted: only the named historian receives the write
        await hass.services.async_call(
            DOMAIN,
            "write",
            {"config_entry_id": entry2.entry_id, "tag": "x", "value": 1},
            blocking=True,
        )
        assert clients[1].async_write.await_count == 1
        assert clients[0].async_write.await_count == 0

        # Unknown id is a validation error
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN,
                "write",
                {"config_entry_id": "nope", "tag": "x", "value": 1},
                blocking=True,
            )

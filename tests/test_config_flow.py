"""Config flow tests."""

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.timebase.api import TimebaseAuthError, TimebaseConnectionError
from custom_components.timebase.const import DOMAIN

BASE_INPUT = {
    "host": "127.0.0.1",
    "port": 4512,
    "dataset": "HomeAssistant",
    "retention_days": 1825,
    "use_ssl": True,
    "verify_ssl": False,
}


async def _start_flow(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_user_flow_creates_entry(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
):
    result = await _start_flow(hass)
    assert result["type"] is FlowResultType.FORM

    with patch(
        "custom_components.timebase.config_flow.TimebaseClient"
    ) as client_cls, patch(
        "custom_components.timebase.async_setup_entry", return_value=True
    ):
        client_cls.return_value.async_get_datasets = AsyncMock(return_value=[])
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], BASE_INPUT
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Timebase (127.0.0.1)"
    assert result["data"]["dataset"] == "HomeAssistant"


async def test_user_flow_cannot_connect(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
):
    result = await _start_flow(hass)
    with patch(
        "custom_components.timebase.config_flow.TimebaseClient"
    ) as client_cls:
        client_cls.return_value.async_get_datasets = AsyncMock(
            side_effect=TimebaseConnectionError("nope")
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], BASE_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_partial_pulse_fields_rejected(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
):
    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**BASE_INPUT, "pulse_url": "https://pulse:4542"},  # id/secret missing
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "pulse_incomplete"}


PULSE_ENTRY_DATA = {
    **BASE_INPUT,
    "pulse_url": "https://pulse:4542",
    "pulse_client_id": "old-id",
    "pulse_client_secret": "old-secret",
}


async def test_reauth_flow_updates_credentials(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
):
    entry = MockConfigEntry(
        domain=DOMAIN, data=PULSE_ENTRY_DATA, unique_id="127.0.0.1:4512:HomeAssistant"
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with patch(
        "custom_components.timebase.config_flow.TimebaseClient"
    ) as client_cls, patch(
        "custom_components.timebase.async_setup_entry", return_value=True
    ):
        client_cls.return_value.async_get_datasets = AsyncMock(return_value=[])
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "pulse_url": "https://pulse:4542",
                "pulse_client_id": "old-id",
                "pulse_client_secret": "new-secret",
            },
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["pulse_client_secret"] == "new-secret"
    assert entry.data["host"] == "127.0.0.1"  # connection fields untouched


async def test_reauth_flow_rejects_bad_credentials(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
):
    entry = MockConfigEntry(
        domain=DOMAIN, data=PULSE_ENTRY_DATA, unique_id="127.0.0.1:4512:HomeAssistant"
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)

    with patch(
        "custom_components.timebase.config_flow.TimebaseClient"
    ) as client_cls:
        client_cls.return_value.async_get_datasets = AsyncMock(
            side_effect=TimebaseAuthError(401, "still wrong")
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "pulse_url": "https://pulse:4542",
                "pulse_client_id": "old-id",
                "pulse_client_secret": "still-wrong",
            },
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data["pulse_client_secret"] == "old-secret"  # unchanged

"""Config flow for the Timebase Historian integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import TimebaseClient, TimebaseConnectionError, TimebaseError
from .auth import PulseAuth, PulseAuthError
from .const import (
    CONF_DATASET,
    CONF_USE_SSL,
    CONF_VERIFY_SSL,
    CONF_EXCLUDE_ENTITY_GLOBS,
    CONF_EXPORT_ENABLED,
    CONF_EXPORT_STRING_STATES,
    CONF_IMPORT_COUNTER_TAGS,
    CONF_IMPORT_TAGS,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_ENTITY_GLOBS,
    CONF_LIVE_TAGS,
    CONF_PULSE_CLIENT_ID,
    CONF_PULSE_CLIENT_SECRET,
    CONF_PULSE_URL,
    CONF_RETENTION_DAYS,
    CONF_TAG_PREFIX,
    DEFAULT_DATASET,
    DEFAULT_EXPORT_ENABLED,
    DEFAULT_PORT,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_TAG_PREFIX,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_DATASET, default=DEFAULT_DATASET): str,
        vol.Required(
            CONF_RETENTION_DAYS, default=DEFAULT_RETENTION_DAYS
        ): vol.All(int, vol.Range(min=1)),
        vol.Required(CONF_USE_SSL, default=False): BooleanSelector(),
        vol.Required(CONF_VERIFY_SSL, default=True): BooleanSelector(),
        vol.Optional(CONF_PULSE_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        ),
        vol.Optional(CONF_PULSE_CLIENT_ID): str,
        vol.Optional(CONF_PULSE_CLIENT_SECRET): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


class TimebaseConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle initial setup of a Timebase historian connection."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(
                f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}:"
                f"{user_input[CONF_DATASET]}"
            )
            self._abort_if_unique_id_configured()

            pulse_url = user_input.get(CONF_PULSE_URL)
            client_id = user_input.get(CONF_PULSE_CLIENT_ID)
            client_secret = user_input.get(CONF_PULSE_CLIENT_SECRET)
            pulse_fields = (pulse_url, client_id, client_secret)
            if any(pulse_fields) and not all(pulse_fields):
                errors["base"] = "pulse_incomplete"
            else:
                session = async_get_clientsession(self.hass)
                verify_ssl = user_input.get(CONF_VERIFY_SSL, True)
                auth = (
                    PulseAuth(
                        session,
                        pulse_url,
                        client_id,
                        client_secret,
                        verify_ssl=verify_ssl,
                    )
                    if pulse_url
                    else None
                )
                try:
                    client = TimebaseClient(
                        session,
                        user_input[CONF_HOST],
                        user_input[CONF_PORT],
                        auth=auth,
                        use_ssl=user_input.get(CONF_USE_SSL, False),
                        verify_ssl=verify_ssl,
                    )
                    await client.async_get_datasets()
                except TimebaseConnectionError:
                    errors["base"] = "cannot_connect"
                except PulseAuthError:
                    errors["base"] = "invalid_auth"
                except TimebaseError as err:
                    if getattr(err, "status", None) in (401, 403):
                        errors["base"] = "invalid_auth"
                    else:
                        errors["base"] = "invalid_response"
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Unexpected error validating Timebase")
                    errors["base"] = "unknown"
                else:
                    return self.async_create_entry(
                        title=f"Timebase ({user_input[CONF_HOST]})",
                        data=user_input,
                    )
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return TimebaseOptionsFlow()


class TimebaseOptionsFlow(OptionsFlow):
    """Options: export filtering and statistics/live-sensor import."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        opts = self.config_entry.options
        multi_text = TextSelector(TextSelectorConfig(multiple=True))
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_EXPORT_ENABLED,
                    default=opts.get(CONF_EXPORT_ENABLED, DEFAULT_EXPORT_ENABLED),
                ): BooleanSelector(),
                vol.Required(
                    CONF_TAG_PREFIX,
                    default=opts.get(CONF_TAG_PREFIX, DEFAULT_TAG_PREFIX),
                ): str,
                vol.Optional(
                    CONF_INCLUDE_DOMAINS,
                    default=opts.get(CONF_INCLUDE_DOMAINS, []),
                ): multi_text,
                vol.Optional(
                    CONF_INCLUDE_ENTITY_GLOBS,
                    default=opts.get(CONF_INCLUDE_ENTITY_GLOBS, []),
                ): multi_text,
                vol.Optional(
                    CONF_EXCLUDE_ENTITY_GLOBS,
                    default=opts.get(CONF_EXCLUDE_ENTITY_GLOBS, []),
                ): multi_text,
                vol.Required(
                    CONF_EXPORT_STRING_STATES,
                    default=opts.get(CONF_EXPORT_STRING_STATES, False),
                ): BooleanSelector(),
                vol.Optional(
                    CONF_IMPORT_TAGS,
                    default=opts.get(CONF_IMPORT_TAGS, []),
                ): multi_text,
                vol.Optional(
                    CONF_IMPORT_COUNTER_TAGS,
                    default=opts.get(CONF_IMPORT_COUNTER_TAGS, []),
                ): multi_text,
                vol.Optional(
                    CONF_LIVE_TAGS,
                    default=opts.get(CONF_LIVE_TAGS, []),
                ): multi_text,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

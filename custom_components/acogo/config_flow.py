"""Config flow for ACO GO integration."""
from __future__ import annotations

import logging
from typing import Any
import uuid

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AcoGoApiClient, AcoGoApiError, AcoGoAuthError
from .const import CONF_DEV_ID, CONF_DEVICE_PASSWORD, CONF_PASSWORD, CONF_USERNAME, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class AcoGoConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for acoGO!."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            dev_id = str(uuid.uuid4())

            session = async_get_clientsession(self.hass)
            client = AcoGoApiClient(session, dev_id=dev_id, username=username, password=password)

            try:
                device_password = await client.register_device(username, password)
                devices = await client.get_device_list()
                if not devices:
                    _LOGGER.warning("No ACO intercom devices found in account")

                await self.async_set_unique_id(username.lower())
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"acoGO ({username})",
                    data={
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                        CONF_DEV_ID: dev_id,
                        CONF_DEVICE_PASSWORD: device_password,
                    },
                )
            except AcoGoAuthError:
                errors["base"] = "invalid_auth"
            except AcoGoApiError:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception during config flow")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

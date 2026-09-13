"""The acoGO! integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AcoGoApiClient
from .const import CONF_DEV_ID, CONF_DEVICE_PASSWORD, CONF_PASSWORD, CONF_USERNAME, DOMAIN
from .coordinator import AcoGoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.LOCK,
    Platform.BUTTON,
    Platform.BINARY_SENSOR,
    Platform.CAMERA,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up acoGO! from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    session = async_get_clientsession(hass)

    def _on_password_updated(new_password: str) -> None:
        """Persist newly rotated devicePassword to ConfigEntry."""
        _LOGGER.info("Persisting rotated acoGO devicePassword to ConfigEntry")
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_DEVICE_PASSWORD: new_password},
        )

    client = AcoGoApiClient(
        session=session,
        dev_id=entry.data[CONF_DEV_ID],
        device_password=entry.data[CONF_DEVICE_PASSWORD],
        username=entry.data.get(CONF_USERNAME),
        password=entry.data.get(CONF_PASSWORD),
        on_device_password_updated=_on_password_updated,
    )

    coordinator = AcoGoDataUpdateCoordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "api": client,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok

"""The acoGO! integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
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
    Platform.SWITCH,
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

    # Start high-frequency line monitor for instant doorbell call detection
    coordinator.start_line_monitor()

    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "api": client,
    }

    # Register WebRTC streaming services
    async def async_handle_start_webrtc_stream(call: ServiceCall) -> dict[str, Any]:
        """Initiate KVS session and return signed WebSocket and ICE configuration to caller."""
        device_id = call.data.get("device_id") or next(iter(coordinator.data), None)
        if not device_id:
            raise HomeAssistantError("Intercom device not found")

        params = await coordinator.async_start_preview(device_id)
        aws = params.get("aws", {})
        if not isinstance(aws, dict):
            raise HomeAssistantError("AWS credentials not available from cloud preview session")

        from .webrtc import generate_signed_wss_url, fetch_ice_servers
        client_id = f"HABrowser_{device_id.replace(':', '')}"
        wss_url = generate_signed_wss_url(aws, client_id)
        ice_servers = await fetch_ice_servers(session, aws)
        ice_list: list[dict[str, Any]] = []
        for s in ice_servers:
            item: dict[str, Any] = {"urls": s.urls}
            if s.username:
                item["username"] = s.username
            if s.credential:
                item["credential"] = s.credential
            ice_list.append(item)

        return {
            "wss_url": wss_url,
            "ice_servers": ice_list,
            "device_id": device_id,
            "timeout": 45,
        }

    hass.services.async_register(
        DOMAIN,
        "start_webrtc_stream",
        async_handle_start_webrtc_stream,
        supports_response=SupportsResponse.ONLY,
    )

    async def async_handle_stop_webrtc_stream(call: ServiceCall) -> dict[str, Any]:
        """Terminate preview session and release intercom line."""
        device_id = call.data.get("device_id") or next(iter(coordinator.data), None)
        if device_id:
            await coordinator.async_stop_preview(device_id)
        return {"status": "ok"}

    hass.services.async_register(
        DOMAIN,
        "stop_webrtc_stream",
        async_handle_stop_webrtc_stream,
        supports_response=SupportsResponse.OPTIONAL,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    data = hass.data[DOMAIN].get(entry.entry_id, {})
    coordinator: AcoGoDataUpdateCoordinator | None = data.get("coordinator")
    if coordinator:
        coordinator.stop_line_monitor()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, "start_webrtc_stream")
            hass.services.async_remove(DOMAIN, "stop_webrtc_stream")
    return unload_ok

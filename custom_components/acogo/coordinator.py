"""DataUpdateCoordinator for ACO GO."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AcoGoApiClient, AcoGoApiError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Auto-close preview after 3 minutes (180s) to prevent intercom line starvation
PREVIEW_WATCHDOG_TIMEOUT = 180.0


class AcoGoDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching ACO GO data from cloud."""

    def __init__(self, hass: HomeAssistant, api: AcoGoApiClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=10),
        )
        self.api = api
        self.preview_active_devices: dict[str, dict[str, Any]] = {}
        self._preview_watchdog_handles: dict[str, asyncio.TimerHandle] = {}

    async def async_start_preview(self, dev_id: str) -> dict[str, Any]:
        """Start live preview session for intercom device with watchdog protection."""
        try:
            resp = await self.api.request_preview(dev_id)
        except AcoGoApiError as err:
            raise HomeAssistantError(f"Ошибка включения камеры {dev_id}: {err}") from err

        params = resp.get("params", {}) if isinstance(resp, dict) else {}
        self.preview_active_devices[dev_id] = params

        # Cancel previous watchdog timer if running
        if dev_id in self._preview_watchdog_handles:
            self._preview_watchdog_handles[dev_id].cancel()

        # Schedule automatic shutdown watchdog (180s)
        loop = self.hass.loop
        self._preview_watchdog_handles[dev_id] = loop.call_later(
            PREVIEW_WATCHDOG_TIMEOUT,
            lambda: self.hass.async_create_task(self.async_stop_preview(dev_id)),
        )

        self.async_update_listeners()
        return params

    async def async_stop_preview(self, dev_id: str) -> None:
        """End live preview session and release intercom line."""
        if dev_id in self._preview_watchdog_handles:
            self._preview_watchdog_handles[dev_id].cancel()
            self._preview_watchdog_handles.pop(dev_id, None)

        try:
            await self.api.end_preview()
        except Exception as err:
            _LOGGER.debug("Error ending preview for %s: %s", dev_id, err)
        finally:
            self.preview_active_devices.pop(dev_id, None)
            self.async_update_listeners()

    def is_preview_active(self, dev_id: str) -> bool:
        """Check if preview is active for device."""
        return dev_id in self.preview_active_devices

    def get_preview_params(self, dev_id: str) -> dict[str, Any]:
        """Get current preview parameters for device."""
        return self.preview_active_devices.get(dev_id, {})

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from ACO GO Cloud with graceful degradation on transient glitches."""
        try:
            devices = await self.api.get_device_list()
            devices_data: dict[str, Any] = {}

            for dev in devices:
                dev_id = dev["devId"]
                try:
                    state_resp = await self.api.check_state(dev_id)
                except Exception as state_err:
                    _LOGGER.warning("Temporary error checking state for %s: %s", dev_id, state_err)
                    prev_state = self.data.get(dev_id, {}).get("state", "ready") if self.data else "ready"
                    state_resp = prev_state

                devices_data[dev_id] = {
                    "info": dev,
                    "state": state_resp,  # 'ready', 'busy', 'offline'
                    "is_online": (state_resp != "offline"),
                    "is_ringing": (state_resp == "busy"),
                }

            return devices_data
        except AcoGoApiError as err:
            if self.data:
                _LOGGER.warning("Temporary error updating ACO GO data (maintaining cached state): %s", err)
                return self.data
            raise UpdateFailed(f"Error communicating with ACO API: {err}") from err

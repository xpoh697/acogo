"""DataUpdateCoordinator for ACO GO."""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AcoGoApiClient, AcoGoApiError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


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

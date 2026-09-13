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
        """Fetch data from ACO GO Cloud."""
        try:
            devices = await self.api.get_device_list()
            devices_data: dict[str, Any] = {}

            for dev in devices:
                dev_id = dev["devId"]
                state_resp = await self.api.check_state(dev_id)
                devices_data[dev_id] = {
                    "info": dev,
                    "state": state_resp,  # 'ready', 'busy', 'offline'
                    "is_online": (state_resp != "offline"),
                    "is_ringing": (state_resp == "busy"),
                }

            return devices_data
        except AcoGoApiError as err:
            raise UpdateFailed(f"Error communicating with ACO API: {err}") from err

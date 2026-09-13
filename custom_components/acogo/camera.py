"""Camera entity for ACO GO video preview."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, PREVIEW_AUTO_CLOSE_TIMEOUT
from .coordinator import AcoGoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ACO GO camera entities."""
    coordinator: AcoGoDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities: list[Camera] = []

    for dev_id in coordinator.data:
        entities.append(AcoGoCamera(coordinator, dev_id))

    async_add_entities(entities)


class AcoGoCamera(CoordinatorEntity[AcoGoDataUpdateCoordinator], Camera):
    """Representation of an ACO GO Intercom Camera."""

    _attr_has_entity_name = True
    _attr_name = "Camera"

    def __init__(self, coordinator: AcoGoDataUpdateCoordinator, dev_id: str) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self.dev_id = dev_id
        self._attr_unique_id = f"{dev_id}_camera"
        self._auto_close_task: asyncio.TimerHandle | None = None
        self._preview_active = False

    @property
    def device_info(self) -> dict[str, Any]:
        info = self.coordinator.data.get(self.dev_id, {}).get("info", {})
        return {
            "identifiers": {(DOMAIN, self.dev_id)},
            "name": info.get("name", f"ACO Intercom {self.dev_id}"),
            "manufacturer": "ACO",
        }

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        """Start preview session and schedule auto-close to avoid cloud quota leaks."""
        try:
            if not self._preview_active:
                await self.coordinator.api.request_preview(self.dev_id)
                self._preview_active = True

            # Reset auto-close timer
            if self._auto_close_task:
                self._auto_close_task.cancel()

            loop = asyncio.get_running_loop()
            self._auto_close_task = loop.call_later(
                PREVIEW_AUTO_CLOSE_TIMEOUT,
                lambda: asyncio.create_task(self._async_close_preview()),
            )
        except Exception as err:
            _LOGGER.debug("Preview request error for %s: %s", self.dev_id, err)

        return None

    async def _async_close_preview(self) -> None:
        """Ensure preview session is closed."""
        if self._preview_active:
            try:
                await self.coordinator.api.end_preview()
            except Exception as err:
                _LOGGER.debug("Error ending preview: %s", err)
            finally:
                self._preview_active = False

    async def async_will_remove_from_hass(self) -> None:
        """Cleanup on entity removal."""
        if self._auto_close_task:
            self._auto_close_task.cancel()
        await self._async_close_preview()
        await super().async_will_remove_from_hass()

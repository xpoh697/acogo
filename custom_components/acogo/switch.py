"""Switch entities for ACO GO camera preview."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AcoGoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ACO GO switch entities."""
    coordinator: AcoGoDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities: list[SwitchEntity] = []
    for dev_id in coordinator.data:
        entities.append(AcoGoCameraPreviewSwitch(coordinator, dev_id))

    async_add_entities(entities)


class AcoGoCameraPreviewSwitch(CoordinatorEntity[AcoGoDataUpdateCoordinator], SwitchEntity):
    """Switch to toggle intercom camera live preview stream."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:video-wireless"

    def __init__(self, coordinator: AcoGoDataUpdateCoordinator, dev_id: str) -> None:
        super().__init__(coordinator)
        self.dev_id = dev_id
        self._attr_unique_id = f"{dev_id}_camera_preview_switch"
        self._attr_name = "Camera Stream"

    @property
    def device_info(self) -> dict[str, Any]:
        info = self.coordinator.data.get(self.dev_id, {}).get("info", {})
        return {
            "identifiers": {(DOMAIN, self.dev_id)},
            "name": info.get("name", f"ACO Intercom {self.dev_id}"),
            "manufacturer": "ACO",
        }

    @property
    def is_on(self) -> bool:
        """Return True if camera preview is active."""
        return self.coordinator.is_preview_active(self.dev_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return safe stream attributes (excluding AWS secret access key)."""
        attrs: dict[str, Any] = {
            "stream_type": "webrtc_kvs",
            "is_streaming": self.coordinator.is_preview_active(self.dev_id),
        }
        params = self.coordinator.get_preview_params(self.dev_id)
        if params:
            attrs["call_id"] = params.get("callId")
            aws_info = params.get("aws", {})
            if isinstance(aws_info, dict):
                attrs["channel_arn"] = aws_info.get("channel arn") or aws_info.get("channelARN")
                attrs["channel_name"] = aws_info.get("channel name")
                attrs["aws_region"] = aws_info.get("region")
                attrs["wss_endpoint"] = aws_info.get("wss endpoint")
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on camera live preview."""
        try:
            await self.coordinator.async_start_preview(self.dev_id)
        except Exception as err:
            raise HomeAssistantError(f"Не удалось включить камеру: {err}") from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off camera live preview."""
        try:
            await self.coordinator.async_stop_preview(self.dev_id)
        except Exception as err:
            raise HomeAssistantError(f"Не удалось выключить камеру: {err}") from err

"""Button entities for impulse open and camera switch."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import AcoGoApiError
from .const import DOMAIN, PRO_MODELS
from .coordinator import AcoGoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ACO GO button entities."""
    coordinator: AcoGoDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities: list[ButtonEntity] = []
    for dev_id in coordinator.data:
        info = coordinator.data.get(dev_id, {}).get("info", {})
        model = info.get("model")
        camera_switching_enabled = bool(info.get("cameraSwitching", False))

        entities.append(AcoGoOpenButton(coordinator, dev_id, is_gate=False))
        entities.append(AcoGoOpenButton(coordinator, dev_id, is_gate=True))

        # Camera switching is only supported on PRO hardware (models 65, 67, 68)
        # with PRO-VIDEO-SW2-60 switcher module or if explicitly enabled.
        if model in PRO_MODELS or camera_switching_enabled:
            entities.append(AcoGoSwitchCameraButton(coordinator, dev_id))
        else:
            _LOGGER.debug(
                "Intercom %s (model %s) does not support camera switching; skipping button entity",
                dev_id,
                model,
            )

    async_add_entities(entities)


class AcoGoOpenButton(CoordinatorEntity[AcoGoDataUpdateCoordinator], ButtonEntity):
    """Button for impulse door/gate opening."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AcoGoDataUpdateCoordinator, dev_id: str, is_gate: bool) -> None:
        super().__init__(coordinator)
        self.dev_id = dev_id
        self.is_gate = is_gate
        suffix = "open_gate" if is_gate else "open_door"
        self._attr_unique_id = f"{dev_id}_{suffix}"
        self._attr_name = "Open Gate" if is_gate else "Open Door"
        self._attr_icon = "mdi:gate" if is_gate else "mdi:door-open"

    @property
    def device_info(self) -> dict[str, Any]:
        info = self.coordinator.data.get(self.dev_id, {}).get("info", {})
        return {
            "identifiers": {(DOMAIN, self.dev_id)},
            "name": info.get("name", f"ACO Intercom {self.dev_id}"),
            "manufacturer": "ACO",
        }

    async def async_press(self) -> None:
        """Trigger opening sequence."""
        try:
            await self.coordinator.api.open_door_sequence(self.dev_id, is_gate=self.is_gate)
        except AcoGoApiError as err:
            action = "ворот" if self.is_gate else "двери"
            raise HomeAssistantError(f"Ошибка открытия {action}: {err}") from err


class AcoGoSwitchCameraButton(CoordinatorEntity[AcoGoDataUpdateCoordinator], ButtonEntity):
    """Button to switch active video input/camera."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:camera-switch"

    def __init__(self, coordinator: AcoGoDataUpdateCoordinator, dev_id: str) -> None:
        super().__init__(coordinator)
        self.dev_id = dev_id
        self._attr_unique_id = f"{dev_id}_switch_camera"
        self._attr_name = "Switch Camera Video Input"

    @property
    def device_info(self) -> dict[str, Any]:
        info = self.coordinator.data.get(self.dev_id, {}).get("info", {})
        return {
            "identifiers": {(DOMAIN, self.dev_id)},
            "name": info.get("name", f"ACO Intercom {self.dev_id}"),
            "manufacturer": "ACO",
        }

    async def async_press(self) -> None:
        """Trigger camera switch."""
        info = self.coordinator.data.get(self.dev_id, {}).get("info", {})
        model = info.get("model", "unknown")
        try:
            await self.coordinator.api.switch_video(self.dev_id)
        except AcoGoApiError as err:
            raise HomeAssistantError(
                f"Переключение камер не поддерживается домофоном {self.dev_id} (модель {model}). "
                "Требуется модель серии PRO (65/67/68) и модуль коммутатора PRO-VIDEO-SW2-60."
            ) from err

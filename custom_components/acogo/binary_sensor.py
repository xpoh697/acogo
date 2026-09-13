"""Binary sensors for online connectivity and incoming calls."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AcoGoDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the ACO GO binary sensors."""
    coordinator: AcoGoDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities: list[BinarySensorEntity] = []
    for dev_id in coordinator.data:
        entities.append(AcoGoConnectivitySensor(coordinator, dev_id))
        entities.append(AcoGoCallSensor(coordinator, dev_id))

    async_add_entities(entities)


class AcoGoConnectivitySensor(CoordinatorEntity[AcoGoDataUpdateCoordinator], BinarySensorEntity):
    """Sensor for device cloud connectivity."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_has_entity_name = True
    _attr_name = "Status"

    def __init__(self, coordinator: AcoGoDataUpdateCoordinator, dev_id: str) -> None:
        super().__init__(coordinator)
        self.dev_id = dev_id
        self._attr_unique_id = f"{dev_id}_connectivity"

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.get(self.dev_id, {}).get("is_online", False)

    @property
    def device_info(self) -> dict[str, Any]:
        info = self.coordinator.data.get(self.dev_id, {}).get("info", {})
        return {
            "identifiers": {(DOMAIN, self.dev_id)},
            "name": info.get("name", f"ACO Intercom {self.dev_id}"),
            "manufacturer": "ACO",
        }


class AcoGoCallSensor(CoordinatorEntity[AcoGoDataUpdateCoordinator], BinarySensorEntity):
    """Sensor for active incoming calls / busy line."""

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_has_entity_name = True
    _attr_name = "Incoming Call / Line Active"
    _attr_icon = "mdi:phone-ring"

    def __init__(self, coordinator: AcoGoDataUpdateCoordinator, dev_id: str) -> None:
        super().__init__(coordinator)
        self.dev_id = dev_id
        self._attr_unique_id = f"{dev_id}_call"

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.get(self.dev_id, {}).get("is_ringing", False)

    @property
    def device_info(self) -> dict[str, Any]:
        info = self.coordinator.data.get(self.dev_id, {}).get("info", {})
        return {
            "identifiers": {(DOMAIN, self.dev_id)},
            "name": info.get("name", f"ACO Intercom {self.dev_id}"),
            "manufacturer": "ACO",
        }

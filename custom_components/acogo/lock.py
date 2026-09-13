"""Lock entities for Door 1 and Gate (F2) on ACO GO."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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
    """Set up the ACO GO lock entities."""
    coordinator: AcoGoDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities: list[LockEntity] = []
    for dev_id, dev_data in coordinator.data.items():
        entities.append(AcoGoLock(coordinator, dev_id, is_gate=False))
        entities.append(AcoGoLock(coordinator, dev_id, is_gate=True))

    async_add_entities(entities)


class AcoGoLock(CoordinatorEntity[AcoGoDataUpdateCoordinator], LockEntity):
    """Representation of an ACO GO Door/Gate lock."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AcoGoDataUpdateCoordinator, dev_id: str, is_gate: bool) -> None:
        super().__init__(coordinator)
        self.dev_id = dev_id
        self.is_gate = is_gate
        suffix = "gate" if is_gate else "door"
        self._attr_unique_id = f"{dev_id}_{suffix}"
        self._attr_name = "Gate (F2)" if is_gate else "Door Lock"
        self._attr_is_locked = True

    @property
    def device_info(self) -> dict[str, Any]:
        info = self.coordinator.data.get(self.dev_id, {}).get("info", {})
        return {
            "identifiers": {(DOMAIN, self.dev_id)},
            "name": info.get("name", f"ACO Intercom {self.dev_id}"),
            "manufacturer": "ACO",
            "model": f"Model {info.get('model', 'Unknown')}",
            "sw_version": info.get("software", "Unknown"),
        }

    @property
    def available(self) -> bool:
        return self.coordinator.data.get(self.dev_id, {}).get("is_online", False)

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock door or gate."""
        self._attr_is_locked = False
        self.async_write_ha_state()

        try:
            await self.coordinator.api.open_door_sequence(self.dev_id, is_gate=self.is_gate)
        finally:
            self._attr_is_locked = True
            self.async_write_ha_state()

    async def async_lock(self, **kwargs: Any) -> None:
        """Electromechanical locks return to locked state automatically."""
        self._attr_is_locked = True
        self.async_write_ha_state()

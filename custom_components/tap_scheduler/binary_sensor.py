"""Binary sensor entities for Logitech Tap Scheduler connectivity."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TapSchedulerCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tap Scheduler binary sensor entities."""
    coordinator: TapSchedulerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TapSchedulerOnlineSensor(coordinator)])


class TapSchedulerOnlineSensor(
    CoordinatorEntity[TapSchedulerCoordinator], BinarySensorEntity
):
    """Indicates whether the Tap Scheduler is actively connected to the proxy."""

    _attr_has_entity_name = True
    _attr_name = "Connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: TapSchedulerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry_id}_connected"
        self._attr_device_info = coordinator.device_info

    @property
    def is_on(self) -> bool:
        return bool((self.coordinator.data or {}).get("online", False))

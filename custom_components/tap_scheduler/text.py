"""Text entity for Logitech Tap Scheduler active URL."""
from __future__ import annotations

from homeassistant.components.text import TextEntity
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
    """Set up Tap Scheduler text entity."""
    coordinator: TapSchedulerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TapSchedulerUrlText(coordinator)])


class TapSchedulerUrlText(CoordinatorEntity[TapSchedulerCoordinator], TextEntity):
    """Represents the currently displayed URL on the Tap Scheduler."""

    _attr_has_entity_name = True
    _attr_name = "Dashboard URL"
    _attr_icon = "mdi:web"
    _attr_native_max = 255

    def __init__(self, coordinator: TapSchedulerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry_id}_url"
        self._attr_device_info = coordinator.device_info

    @property
    def native_value(self) -> str:
        return str((self.coordinator.data or {}).get("url", ""))

    async def async_set_value(self, value: str) -> None:
        if value.strip():
            await self.coordinator.async_send_control({"url": value.strip()})

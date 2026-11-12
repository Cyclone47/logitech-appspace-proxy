"""Switch entities for Logitech Tap Scheduler hidden features (Burn-in Protection)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
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
    """Set up Tap Scheduler switch entities."""
    coordinator: TapSchedulerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TapSchedulerBurnInSwitch(coordinator)])


class TapSchedulerBurnInSwitch(CoordinatorEntity[TapSchedulerCoordinator], SwitchEntity):
    """Switch to toggle periodic 1px Anti-Burn-In pixel shifting."""

    _attr_has_entity_name = True
    _attr_name = "Anti-Burn-In Pixel Shift"
    _attr_icon = "mdi:shield-sun-outline"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: TapSchedulerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry_id}_burn_in_protection"
        self._attr_device_info = coordinator.device_info

    @property
    def is_on(self) -> bool:
        return bool((self.coordinator.data or {}).get("burn_in_protection", True))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_control({"burn_in_protection": True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_control({"burn_in_protection": False})

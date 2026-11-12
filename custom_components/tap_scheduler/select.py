"""Select entities for Logitech Tap Scheduler (Status LED Mode & Resolution Zoom)."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TapSchedulerCoordinator

LED_OPTIONS = {
    "off": ("Off", False, [0, 0, 0]),
    "available": ("Green (Available)", True, [34, 197, 94]),
    "occupied": ("Red (Occupied)", True, [239, 68, 68]),
    "checkin": ("Yellow (Check-In)", True, [234, 179, 8]),
    "purple": ("Purple", True, [168, 85, 247]),
    "ada_available": ("Blue (ADA Available)", True, [59, 130, 246]),
    "ada_occupied": ("Orange (ADA Occupied)", True, [249, 115, 22]),
}

ZOOM_PRESETS = {
    "50% (1920p)": 0.50,
    "60% (1420p)": 0.60,
    "67% (1280p)": 0.67,
    "75%": 0.75,
    "100%": 1.00,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tap Scheduler select entities."""
    coordinator: TapSchedulerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            TapSchedulerLedSelect(coordinator),
            TapSchedulerZoomSelect(coordinator),
        ]
    )


class TapSchedulerLedSelect(CoordinatorEntity[TapSchedulerCoordinator], SelectEntity):
    """Select preset LED / Edge Glow status color."""

    _attr_has_entity_name = True
    _attr_name = "Status LED Mode"
    _attr_icon = "mdi:alarm-light-outline"
    _attr_options = [v[0] for v in LED_OPTIONS.values()]

    def __init__(self, coordinator: TapSchedulerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry_id}_led_mode"
        self._attr_device_info = coordinator.device_info

    @property
    def current_option(self) -> str:
        mode = str((self.coordinator.data or {}).get("led_mode", "off"))
        return LED_OPTIONS.get(mode, LED_OPTIONS["off"])[0]

    async def async_select_option(self, option: str) -> None:
        for mode_key, (label, glow_on, rgb) in LED_OPTIONS.items():
            if label == option:
                await self.coordinator.async_send_control(
                    {
                        "led_mode": mode_key,
                        "glow_on": glow_on,
                        "glow_color": rgb,
                    }
                )
                return


class TapSchedulerZoomSelect(CoordinatorEntity[TapSchedulerCoordinator], SelectEntity):
    """Select desktop resolution scaling preset."""

    _attr_has_entity_name = True
    _attr_name = "Resolution Scale"
    _attr_icon = "mdi:magnify-scan"
    _attr_options = list(ZOOM_PRESETS.keys())

    def __init__(self, coordinator: TapSchedulerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry_id}_zoom_preset"
        self._attr_device_info = coordinator.device_info

    @property
    def current_option(self) -> str:
        z = float((self.coordinator.data or {}).get("zoom", 0.67))
        closest_label = "67% (1280p)"
        best_diff = 999.0
        for label, val in ZOOM_PRESETS.items():
            diff = abs(val - z)
            if diff < best_diff:
                best_diff = diff
                closest_label = label
        return closest_label

    async def async_select_option(self, option: str) -> None:
        if option in ZOOM_PRESETS:
            await self.coordinator.async_send_control({"zoom": ZOOM_PRESETS[option]})

"""Select entities for Logitech Tap Scheduler (Unfoldable Zoom, Auto-Sleep & Screensaver)."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TapSchedulerCoordinator

ZOOM_PRESETS: dict[str, float] = {
    "50% (1706×1066)": 0.50,
    "55% (1551×969)": 0.55,
    "60% (1422×888)": 0.60,
    "67% (1280×800 Recommended)": 0.67,
    "75% (1138×711)": 0.75,
    "85% (1004×627)": 0.85,
    "90% (948×592)": 0.90,
    "100% (853×533 Native)": 1.00,
}

AUTO_SLEEP_OPTIONS: dict[str, int] = {
    "Disabled": 0,
    "1 Minute": 1,
    "5 Minutes": 5,
    "10 Minutes": 10,
    "15 Minutes": 15,
    "30 Minutes": 30,
    "60 Minutes": 60,
}

SCREENSAVER_OPTIONS: dict[str, str] = {
    "Pure Black (Screen Off)": "black",
    "Floating Digital Clock": "clock",
    "Dimmed Dashboard (5%)": "dim",
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
            TapSchedulerZoomSelect(coordinator),
            TapSchedulerAutoSleepSelect(coordinator),
            TapSchedulerScreensaverSelect(coordinator),
        ]
    )


class TapSchedulerZoomSelect(CoordinatorEntity[TapSchedulerCoordinator], SelectEntity):
    """Single unfoldable dropdown for Display Zoom & Resolution scaling."""

    _attr_has_entity_name = True
    _attr_name = "Display Zoom & Resolution"
    _attr_icon = "mdi:magnify-scan"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(ZOOM_PRESETS.keys())

    def __init__(self, coordinator: TapSchedulerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry_id}_zoom_preset"
        self._attr_device_info = coordinator.device_info

    @property
    def current_option(self) -> str:
        z = float((self.coordinator.data or {}).get("zoom", 0.67))
        closest_label = "67% (1280×800 Recommended)"
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


class TapSchedulerAutoSleepSelect(CoordinatorEntity[TapSchedulerCoordinator], SelectEntity):
    """Unfoldable dropdown for automatic inactivity screen sleep timeout."""

    _attr_has_entity_name = True
    _attr_name = "Auto-Sleep Timeout"
    _attr_icon = "mdi:timer-sleep-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(AUTO_SLEEP_OPTIONS.keys())

    def __init__(self, coordinator: TapSchedulerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry_id}_auto_sleep"
        self._attr_device_info = coordinator.device_info

    @property
    def current_option(self) -> str:
        mins = int((self.coordinator.data or {}).get("auto_sleep_minutes", 0))
        for label, val in AUTO_SLEEP_OPTIONS.items():
            if val == mins:
                return label
        return "Disabled"

    async def async_select_option(self, option: str) -> None:
        if option in AUTO_SLEEP_OPTIONS:
            await self.coordinator.async_send_control(
                {"auto_sleep_minutes": AUTO_SLEEP_OPTIONS[option]}
            )


class TapSchedulerScreensaverSelect(CoordinatorEntity[TapSchedulerCoordinator], SelectEntity):
    """Unfoldable dropdown for Sleep / Screensaver display style."""

    _attr_has_entity_name = True
    _attr_name = "Sleep / Screensaver Style"
    _attr_icon = "mdi:weather-night"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(SCREENSAVER_OPTIONS.keys())

    def __init__(self, coordinator: TapSchedulerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry_id}_screensaver_mode"
        self._attr_device_info = coordinator.device_info

    @property
    def current_option(self) -> str:
        mode = str((self.coordinator.data or {}).get("screensaver_mode", "black"))
        for label, val in SCREENSAVER_OPTIONS.items():
            if val == mode:
                return label
        return "Pure Black (Screen Off)"

    async def async_select_option(self, option: str) -> None:
        if option in SCREENSAVER_OPTIONS:
            await self.coordinator.async_send_control(
                {"screensaver_mode": SCREENSAVER_OPTIONS[option]}
            )

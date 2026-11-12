"""Light entities for Logitech Tap Scheduler (Screen Display & Ambient Edge Glow)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TapSchedulerCoordinator

EFFECT_PRESET_MAP: dict[str, tuple[str, list[int], str]] = {
    "Preset: Available (Green)": ("available", [34, 197, 94], "solid"),
    "Preset: Occupied (Red)": ("occupied", [239, 68, 68], "solid"),
    "Preset: Check-In (Yellow)": ("checkin", [234, 179, 8], "solid"),
    "Preset: Purple": ("purple", [168, 85, 247], "solid"),
    "Preset: ADA Blue": ("ada_available", [59, 130, 246], "solid"),
    "Preset: ADA Orange": ("ada_occupied", [249, 115, 22], "solid"),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tap Scheduler light entities."""
    coordinator: TapSchedulerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            TapSchedulerScreenLight(coordinator),
            TapSchedulerEdgeGlowLight(coordinator),
        ]
    )


class TapSchedulerScreenLight(CoordinatorEntity[TapSchedulerCoordinator], LightEntity):
    """Represents the Tap Scheduler screen power (sleep/wake) and brightness dimmer."""

    _attr_has_entity_name = True
    _attr_name = "Screen"
    _attr_icon = "mdi:tablet-dashboard"
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(self, coordinator: TapSchedulerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry_id}_screen"
        self._attr_device_info = coordinator.device_info

    @property
    def is_on(self) -> bool:
        return bool((self.coordinator.data or {}).get("screen_on", True))

    @property
    def brightness(self) -> int:
        pct = int((self.coordinator.data or {}).get("brightness", 100))
        return max(1, min(255, round(pct * 255 / 100)))

    async def async_turn_on(self, **kwargs: Any) -> None:
        payload: dict[str, Any] = {"screen_on": True}
        if ATTR_BRIGHTNESS in kwargs:
            pct = max(1, min(100, round(kwargs[ATTR_BRIGHTNESS] * 100 / 255)))
            payload["brightness"] = pct
        await self.coordinator.async_send_control(payload)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_control({"screen_on": False})


class TapSchedulerEdgeGlowLight(CoordinatorEntity[TapSchedulerCoordinator], LightEntity):
    """Represents the Tap Scheduler RGB LED / Ambient Edge Glow frame with unfoldable presets."""

    _attr_has_entity_name = True
    _attr_name = "LED & Edge Glow"
    _attr_icon = "mdi:led-strip-variant"
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = [
        "solid",
        "pulse",
        "breathe",
        "flash",
        *EFFECT_PRESET_MAP.keys(),
    ]

    def __init__(self, coordinator: TapSchedulerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry_id}_edge_glow"
        self._attr_device_info = coordinator.device_info

    @property
    def is_on(self) -> bool:
        return bool((self.coordinator.data or {}).get("glow_on", False))

    @property
    def rgb_color(self) -> tuple[int, int, int]:
        rgb = (self.coordinator.data or {}).get("glow_color", [168, 85, 247])
        if isinstance(rgb, list) and len(rgb) == 3:
            return (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        return (168, 85, 247)

    @property
    def effect(self) -> str:
        return str((self.coordinator.data or {}).get("glow_effect", "solid"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        payload: dict[str, Any] = {"glow_on": True}
        if ATTR_EFFECT in kwargs:
            eff = str(kwargs[ATTR_EFFECT])
            if eff in EFFECT_PRESET_MAP:
                mode_key, rgb, anim = EFFECT_PRESET_MAP[eff]
                payload["led_mode"] = mode_key
                payload["glow_color"] = rgb
                payload["glow_effect"] = anim
            else:
                payload["glow_effect"] = eff
        if ATTR_RGB_COLOR in kwargs:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            payload["glow_color"] = [int(r), int(g), int(b)]
            if g > r * 1.4 and g > b * 1.4:
                payload["led_mode"] = "available"
            elif r > g * 1.4 and r > b * 1.4:
                payload["led_mode"] = "occupied"
            elif r > 150 and g > 120 and b < 80:
                payload["led_mode"] = "checkin"
            elif b > r * 1.3 and b > g * 1.3:
                payload["led_mode"] = "ada_available"
            else:
                payload["led_mode"] = "purple"
        await self.coordinator.async_send_control(payload)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_control({"glow_on": False, "led_mode": "off"})

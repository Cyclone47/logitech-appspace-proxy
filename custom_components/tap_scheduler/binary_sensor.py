"""Binary sensor entities for Logitech Tap Scheduler (Touch Occupancy & Connectivity)."""
from __future__ import annotations

import time
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
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
    """Set up Tap Scheduler binary sensor entities."""
    coordinator: TapSchedulerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            TapSchedulerTouchPresenceSensor(coordinator),
            TapSchedulerOnlineSensor(coordinator),
        ]
    )


class TapSchedulerTouchPresenceSensor(
    CoordinatorEntity[TapSchedulerCoordinator], BinarySensorEntity
):
    """Occupancy / Touch sensor that activates when someone touches or wakes the Tap Scheduler."""

    _attr_has_entity_name = True
    _attr_name = "Screen Touch Activity"
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_icon = "mdi:gesture-tap"

    def __init__(self, coordinator: TapSchedulerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry_id}_touch_activity"
        self._attr_device_info = coordinator.device_info

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data or {}
        if "touched_recently" in data:
            return bool(data["touched_recently"])
        last_touch = float(data.get("last_touch") or 0.0)
        return (time.time() - last_touch) < 60 if last_touch > 0 else False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        last_touch = float(data.get("last_touch") or 0.0)
        return {
            "seconds_since_last_touch": round(time.time() - last_touch, 1) if last_touch > 0 else None,
            "auto_sleep_minutes": data.get("auto_sleep_minutes", 0),
            "screensaver_mode": data.get("screensaver_mode", "black"),
        }


class TapSchedulerOnlineSensor(
    CoordinatorEntity[TapSchedulerCoordinator], BinarySensorEntity
):
    """Indicates whether the Tap Scheduler is actively connected to the proxy, with hardware diagnostics."""

    _attr_has_entity_name = True
    _attr_name = "Connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: TapSchedulerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry_id}_connected"
        self._attr_device_info = coordinator.device_info

    @property
    def is_on(self) -> bool:
        return bool((self.coordinator.data or {}).get("online", False))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        caps = (data.get("appspace_capabilities") or {}).get("config") or {}
        dev_id = caps.get("deviceIdentity") or {}
        hw = dev_id.get("hardware") or {}
        sw = dev_id.get("software") or {}
        last_seen = float(data.get("last_seen") or 0.0)
        return {
            "tap_ip": data.get("tap_ip"),
            "serial_number": hw.get("serialNumber"),
            "hardware_model": hw.get("model"),
            "resolution": hw.get("resolution"),
            "memory": hw.get("memory"),
            "storage": hw.get("storage"),
            "collabos_firmware": sw.get("firmware"),
            "appspace_version": sw.get("appVersion"),
            "active_url": data.get("url"),
            "color_palette": data.get("color_palette"),
            "seconds_since_last_poll": round(time.time() - last_seen, 1) if last_seen > 0 else None,
        }

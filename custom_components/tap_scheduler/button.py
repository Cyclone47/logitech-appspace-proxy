"""Button entities for Logitech Tap Scheduler actions & hidden menus."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TapSchedulerCoordinator

BUTTON_DEFINITIONS: list[tuple[str, str, str, EntityCategory | None]] = [
    ("flash_alert", "Flash Visual Alert", "mdi:alarm-light", None),
    ("return_home", "Return to Home Dashboard", "mdi:home-import-outline", None),
    ("reload", "Reload Dashboard", "mdi:refresh", None),
    ("open_collabos_settings", "Open Tap CollabOS Web UI", "mdi:cog-outline", EntityCategory.CONFIG),
    ("open_device_menu", "Open Native Logitech / Appspace Menu", "mdi:cog-transfer-outline", EntityCategory.CONFIG),
    ("resync_app", "Resync Appspace Config & Palette", "mdi:sync", EntityCategory.CONFIG),
    ("restart_app", "Restart Appspace Player", "mdi:restart", EntityCategory.CONFIG),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tap Scheduler button entities."""
    coordinator: TapSchedulerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            TapSchedulerActionButton(coordinator, action_key, label, icon, category)
            for action_key, label, icon, category in BUTTON_DEFINITIONS
        ]
    )


class TapSchedulerActionButton(CoordinatorEntity[TapSchedulerCoordinator], ButtonEntity):
    """Button to trigger an immediate action or hidden menu on the Tap Scheduler."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TapSchedulerCoordinator,
        action_key: str,
        label: str,
        icon: str,
        category: EntityCategory | None,
    ) -> None:
        super().__init__(coordinator)
        self._action_key = action_key
        self._attr_name = label
        self._attr_icon = icon
        self._attr_entity_category = category
        self._attr_unique_id = f"{coordinator.entry_id}_btn_{action_key}"
        self._attr_device_info = coordinator.device_info

    async def async_press(self) -> None:
        await self.coordinator.async_send_control({"action": self._action_key})

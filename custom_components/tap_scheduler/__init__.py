"""The Logitech Tap Scheduler integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall

from .const import CONF_ADDON_URL, DEFAULT_ADDON_URL, DOMAIN
from .coordinator import TapSchedulerCoordinator

PLATFORMS: list[Platform] = [
    Platform.LIGHT,
    Platform.SELECT,
    Platform.BUTTON,
    Platform.TEXT,
    Platform.BINARY_SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Logitech Tap Scheduler from a config entry."""
    addon_url = entry.data.get(CONF_ADDON_URL, DEFAULT_ADDON_URL)
    coordinator = TapSchedulerCoordinator(hass, addon_url, entry.entry_id)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def handle_popup_url(call: ServiceCall) -> None:
        url = call.data.get("url")
        duration = call.data.get("duration", 60)
        for coord in hass.data[DOMAIN].values():
            await coord.async_send_control({"url": url, "duration": duration, "screen_on": True})

    async def handle_show_toast(call: ServiceCall) -> None:
        message = call.data.get("message")
        for coord in hass.data[DOMAIN].values():
            await coord.async_send_control({"toast": message, "screen_on": True})

    if not hass.services.has_service(DOMAIN, "popup_url"):
        hass.services.async_register(DOMAIN, "popup_url", handle_popup_url)
    if not hass.services.has_service(DOMAIN, "show_toast"):
        hass.services.async_register(DOMAIN, "show_toast", handle_show_toast)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok

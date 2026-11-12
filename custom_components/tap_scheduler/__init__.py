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
    Platform.SWITCH,
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
        duration = call.data.get("duration", call.data.get("revert_seconds", 60))
        for coord in hass.data[DOMAIN].values():
            await coord.async_send_control({"url": url, "duration": duration, "screen_on": True})

    async def handle_show_toast(call: ServiceCall) -> None:
        message = call.data.get("message")
        duration = call.data.get("duration", 6)
        for coord in hass.data[DOMAIN].values():
            await coord.async_send_control({"toast": message, "duration": duration, "screen_on": True})

    async def handle_flash_alert(call: ServiceCall) -> None:
        color = call.data.get("rgb_color", [239, 68, 68])
        for coord in hass.data[DOMAIN].values():
            await coord.async_send_control({"action": "flash_alert", "color": color, "screen_on": True})

    async def handle_open_device_menu(call: ServiceCall) -> None:
        for coord in hass.data[DOMAIN].values():
            await coord.async_send_control({"action": "open_device_menu", "screen_on": True})

    for svc_name, handler in (
        ("popup_url", handle_popup_url),
        ("load_url", handle_popup_url),
        ("show_toast", handle_show_toast),
        ("notify", handle_show_toast),
        ("flash_alert", handle_flash_alert),
        ("open_device_menu", handle_open_device_menu),
    ):
        if not hass.services.has_service(DOMAIN, svc_name):
            hass.services.async_register(DOMAIN, svc_name, handler)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok

"""DataUpdateCoordinator for the Logitech Tap Scheduler integration."""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class TapSchedulerCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to manage polling and sending commands to the Tap Scheduler Add-on."""

    def __init__(self, hass: HomeAssistant, addon_url: str, entry_id: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Logitech Tap Scheduler",
            update_interval=timedelta(seconds=3),
        )
        self.addon_url = addon_url.rstrip("/")
        self.entry_id = entry_id
        self.session = async_get_clientsession(hass)

    @property
    def device_info(self) -> DeviceInfo:
        data = self.data or {}
        tap_ip = data.get("tap_ip") or "192.168.178.178"
        caps = (data.get("appspace_capabilities") or {}).get("config") or {}
        dev_id = caps.get("deviceIdentity") or {}
        hw = dev_id.get("hardware") or {}
        sw = dev_id.get("software") or {}

        serial = hw.get("serialNumber") or None
        fw = sw.get("firmware") or "2.0.86"
        app_ver = sw.get("appVersion") or "2.89.2"
        res = hw.get("resolution") or "1280 x 800"
        mem = hw.get("memory") or "4.0 GB"

        return DeviceInfo(
            identifiers={(DOMAIN, self.entry_id)},
            name=f"Logitech Tap Scheduler ({tap_ip})",
            manufacturer="Logitech",
            model="Tap Scheduler (VR0028 / CollabOS)",
            serial_number=serial,
            sw_version=f"CollabOS {fw} · Appspace {app_ver}",
            hw_version=f"{res} · {mem} RAM",
            configuration_url=self.addon_url,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            async with self.session.get(
                f"{self.addon_url}/api/state",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    raise UpdateFailed(f"HTTP {resp.status} from {self.addon_url}/api/state")
                return await resp.json()
        except Exception as err:
            raise UpdateFailed(f"Error communicating with Tap Scheduler Add-on: {err}") from err

    async def async_send_control(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a control payload to POST /api/control and update coordinator data immediately."""
        async with self.session.post(
            f"{self.addon_url}/api/control",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            data = await resp.json()
            self.async_set_updated_data(data)
            return data

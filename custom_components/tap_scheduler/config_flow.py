"""Config flow for Logitech Tap Scheduler integration."""
from __future__ import annotations

from typing import Any
import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_ADDON_URL, DEFAULT_ADDON_URL, DOMAIN


class TapSchedulerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Logitech Tap Scheduler."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            addon_url = user_input[CONF_ADDON_URL].rstrip("/")
            session = async_get_clientsession(self.hass)
            try:
                async with session.get(
                    f"{addon_url}/api/state",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        return self.async_create_entry(
                            title="Logitech Tap Scheduler",
                            data={CONF_ADDON_URL: addon_url},
                        )
                    errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDON_URL, default=DEFAULT_ADDON_URL): str,
                }
            ),
            errors=errors,
        )

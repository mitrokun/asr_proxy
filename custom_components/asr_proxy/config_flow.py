# --- config_flow.py ---

from typing import Any
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .api import SttApi, CannotConnect
from .const import (
    DOMAIN,
    CONF_STT_HOST,
    CONF_STT_PORT,
    CONF_FALLBACK_HOST,
    CONF_FALLBACK_PORT,
    DEFAULT_STT_HOST,
    DEFAULT_STT_PORT,
)

class AsrProxyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configuration flow for ASR Proxy."""
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            # Validate only the primary server
            primary_host = user_input[CONF_STT_HOST]
            primary_port = user_input[CONF_STT_PORT]
            
            await self.async_set_unique_id(f"{primary_host}:{primary_port}")
            self._abort_if_unique_id_configured()

            try:
                api = SttApi(primary_host, primary_port)
                await api.connect_and_get_info()
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=f"ASR Proxy ({primary_host})", data=user_input
                )
        
        data_schema = vol.Schema({
            vol.Required(CONF_STT_HOST, default=DEFAULT_STT_HOST): str,
            vol.Required(CONF_STT_PORT, default=DEFAULT_STT_PORT): int,
            vol.Optional(CONF_FALLBACK_HOST): str,
            vol.Optional(CONF_FALLBACK_PORT): int,
        })

        return self.async_show_form(step_id="user", data_schema=data_schema, errors=errors)
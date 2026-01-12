from typing import Any
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, ConfigEntry, OptionsFlowWithConfigEntry

from .api import SttApi, CannotConnect
from .const import (
    DOMAIN,
    CONF_STT_HOST,
    CONF_STT_PORT,
    CONF_FALLBACK_HOST,
    CONF_FALLBACK_PORT,
    CONF_SPEECH_TO_PHRASE,  # Наша новая опция
    DEFAULT_STT_HOST,
    DEFAULT_STT_PORT,
)

class AsrProxyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configuration flow for ASR Proxy."""
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlowWithConfigEntry:
        """Get the options flow for this handler."""
        return AsrProxyOptionsFlow(config_entry)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial setup step."""
        errors: dict[str, str] = {}
        if user_input is not None:
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


class AsrProxyOptionsFlow(OptionsFlowWithConfigEntry):
    """Handle an options flow for ASR Proxy."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate the new primary server settings
            primary_host = user_input[CONF_STT_HOST]
            primary_port = user_input[CONF_STT_PORT]
            try:
                api = SttApi(primary_host, primary_port)
                await api.connect_and_get_info()
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                # Create a new entry with the updated options.
                return self.async_create_entry(title="", data=user_input)

        # Build form, pre-filling with current values from options or initial data.
        data_schema = vol.Schema({
            vol.Optional(
                CONF_SPEECH_TO_PHRASE,
                description={"suggested_value": self.options.get(CONF_SPEECH_TO_PHRASE, False)},
                default=False
            ): bool,
            vol.Required(
                CONF_STT_HOST,
                default=self.options.get(
                    CONF_STT_HOST, self.config_entry.data.get(CONF_STT_HOST)
                ),
            ): str,
            vol.Required(
                CONF_STT_PORT,
                default=self.options.get(
                    CONF_STT_PORT, self.config_entry.data.get(CONF_STT_PORT)
                ),
            ): int,
            vol.Optional(
                CONF_FALLBACK_HOST,
                description={"suggested_value": self.options.get(
                    CONF_FALLBACK_HOST, self.config_entry.data.get(CONF_FALLBACK_HOST)
                )},
            ): str,
            vol.Optional(
                CONF_FALLBACK_PORT,
                description={"suggested_value": self.options.get(
                    CONF_FALLBACK_PORT, self.config_entry.data.get(CONF_FALLBACK_PORT)
                )},
            ): int,
        })

        return self.async_show_form(
            step_id="init", data_schema=data_schema, errors=errors
        )
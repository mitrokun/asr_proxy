from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN, 
    CONF_STT_HOST, 
    CONF_STT_PORT, 
    CONF_FALLBACK_HOST, 
    CONF_FALLBACK_PORT
)
from .api import SttApi

PLATFORMS: list[str] = ["stt"]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the integration from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    
    # Merge data and options
    config = {**entry.data, **entry.options}
    
    # Create API client for the primary server
    primary_api = SttApi(
        host=config[CONF_STT_HOST],
        port=config[CONF_STT_PORT]
    )
    
    # Create API client for the fallback server, if configured
    fallback_api = None
    if config.get(CONF_FALLBACK_HOST) and config.get(CONF_FALLBACK_PORT):
        fallback_api = SttApi(
            host=config[CONF_FALLBACK_HOST],
            port=config[CONF_FALLBACK_PORT]
        )
    
    # Store clients in hass.data
    hass.data[DOMAIN][entry.entry_id] = {
        "primary_api": primary_api,
        "fallback_api": fallback_api,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    # Listener for reloading on options update
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_forward_entry_unload(entry, "stt"):
        hass.data[DOMAIN].pop(entry.entry_id)
    
    return unload_ok

async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload a config entry on update."""
    await hass.config_entries.async_reload(entry.entry_id)
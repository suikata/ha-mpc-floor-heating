"""MPC Floor Heating: predictive supply temperature control for floor heating."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later

from .coordinator import MpcConfigEntry, MpcCoordinator

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.NUMBER, Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: MpcConfigEntry) -> bool:
    """Set up from a config entry."""
    coordinator = MpcCoordinator(hass, entry)
    await coordinator.async_load()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start()

    # the number and switch entities restore their values while the platforms
    # load; recompute once they are all in place
    async def _refresh(_now) -> None:
        await coordinator.async_request_refresh()

    entry.async_on_unload(async_call_later(hass, 30, _refresh))
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: MpcConfigEntry) -> None:
    """Reload after the options changed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: MpcConfigEntry) -> bool:
    """Unload a config entry.

    In offset mode the offset goes back to its reset value whenever the
    integration is unloaded (reload, disable, removal), so the heating falls
    back to its own heating curve. A shutdown of Home Assistant does not unload
    integrations; a watchdog in the heating controller covers that case.
    """
    coordinator = entry.runtime_data
    if coordinator.active:
        await coordinator.async_release()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

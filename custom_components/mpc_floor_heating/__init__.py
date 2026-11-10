"""MPC Floor Heating: predictive supply temperature control for floor heating."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.start import async_at_started

from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_CONTROL_MODE,
    CONF_ROOM_SENSORS,
    CONF_TARGET,
    CONTROL_MODES,
    MODE_SENSOR,
    STARTUP_HOLD_SECONDS,
)
from .coordinator import MpcConfigEntry, MpcCoordinator

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.NUMBER, Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: MpcConfigEntry) -> bool:
    """Set up from a config entry."""
    _remove_stale_entities(hass, entry)
    coordinator = MpcCoordinator(hass, entry)
    await coordinator.async_load()
    # after a restart thermostats and sensors report late; a plan made from
    # half of them would be wrong, so control waits until they are in
    coordinator.startup_hold = not hass.is_running
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start()

    # the number and switch entities restore their values while the platforms
    # load; recompute once they are all in place
    async def _refresh(_now) -> None:
        await coordinator.async_request_refresh()

    entry.async_on_unload(async_call_later(hass, 30, _refresh))

    async def _release(_now) -> None:
        coordinator.startup_hold = False
        await coordinator.async_request_refresh()

    @callback
    def _started(_hass: HomeAssistant) -> None:
        if coordinator.startup_hold:
            entry.async_on_unload(async_call_later(hass, STARTUP_HOLD_SECONDS, _release))

    if (cancel_started := async_at_started(hass, _started)) is not None:
        entry.async_on_unload(cancel_started)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


STALE_KEYS = ("comfort_min", "comfort_max")  # replaced by per-room set points in 0.3


def _remove_stale_entities(hass: HomeAssistant, entry: MpcConfigEntry) -> None:
    ent_reg = er.async_get(hass)
    for key in STALE_KEYS:
        for domain in ("number",):
            if eid := ent_reg.async_get_entity_id(domain, "mpc_floor_heating", f"{entry.entry_id}_{key}"):
                ent_reg.async_remove(eid)


async def _async_update_listener(hass: HomeAssistant, entry: MpcConfigEntry) -> None:
    """Reload after the options changed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: MpcConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: MpcConfigEntry) -> bool:
    """1 -> 2: offset mode replaced by the set point sensor. 2 -> 3: thermostats as rooms."""
    if entry.version == 1:
        options = dict(entry.options)
        for key in ("setpoint_sensor", "offset_reset"):
            options.pop(key, None)
        if options.get(CONF_CONTROL_MODE) not in CONTROL_MODES:
            options[CONF_CONTROL_MODE] = MODE_SENSOR
            options.pop(CONF_TARGET, None)
        hass.config_entries.async_update_entry(entry, options=options, version=2)
    if entry.version == 2:
        # 0.3: rooms are preferably thermostats; swap each temperature sensor for
        # the thermostat on the same device so its set point is used
        options = dict(entry.options)
        ent_reg = er.async_get(hass)
        rooms = []
        for eid in options.get(CONF_ROOM_SENSORS, []):
            entry_ = ent_reg.async_get(eid)
            swap = None
            if entry_ is not None and entry_.device_id and eid.startswith("sensor."):
                swap = next(
                    (e.entity_id for e in er.async_entries_for_device(ent_reg, entry_.device_id) if e.domain == "climate"),
                    None,
                )
            rooms.append(swap or eid)
        options[CONF_ROOM_SENSORS] = list(dict.fromkeys(rooms))
        hass.config_entries.async_update_entry(entry, options=options, version=3)
    return True

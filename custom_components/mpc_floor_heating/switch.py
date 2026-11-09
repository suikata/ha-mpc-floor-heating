"""Switch between shadow mode (calculate only) and active control."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import MpcConfigEntry
from .entity import MpcEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: MpcConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the control switch."""
    async_add_entities([MpcActiveSwitch(entry.runtime_data, "control_active")])


class MpcActiveSwitch(MpcEntity, SwitchEntity, RestoreEntity):
    """On: the set point is written to the heating. Off: shadow mode."""

    _attr_icon = "mdi:robot-outline"

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.active

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        # restore without writing immediately, the next run takes over
        self.coordinator.active = last is not None and last.state == STATE_ON

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_active(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_active(False)
        self.async_write_ha_state()

"""Buttons: retrain the model, recalculate now."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import MpcConfigEntry
from .entity import MpcEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: MpcConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add buttons."""
    coordinator = entry.runtime_data
    async_add_entities([RetrainButton(coordinator, "retrain"), RecalculateButton(coordinator, "recalculate")])


class RetrainButton(MpcEntity, ButtonEntity):
    """Learn the building model again from the recorder."""

    _attr_icon = "mdi:school-outline"
    _attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        await self.coordinator.async_retrain()


class RecalculateButton(MpcEntity, ButtonEntity):
    """Run the optimisation now instead of waiting for hh:07."""

    _attr_icon = "mdi:calculator-variant-outline"

    async def async_press(self) -> None:
        await self.coordinator.async_refresh()

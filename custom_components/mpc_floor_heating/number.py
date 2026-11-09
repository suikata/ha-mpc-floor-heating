"""Tunable settings as number entities (kept across restarts)."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntityDescription,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import MpcConfigEntry, MpcCoordinator
from .entity import MpcEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class MpcNumberDescription(NumberEntityDescription):
    """Number bound to a field of MpcSettings."""

    field: str


def _temp(key: str, field: str, low: float, high: float, step: float = 0.1, icon: str | None = None) -> MpcNumberDescription:
    return MpcNumberDescription(
        key=key,
        field=field,
        icon=icon,
        native_min_value=low,
        native_max_value=high,
        native_step=step,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=NumberDeviceClass.TEMPERATURE,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
    )


NUMBERS: tuple[MpcNumberDescription, ...] = (
    MpcNumberDescription(
        key="tolerance",
        field="tolerance",
        icon="mdi:plus-minus-variant",
        native_min_value=0.1,
        native_max_value=3.0,
        native_step=0.1,
        native_unit_of_measurement=UnitOfTemperature.KELVIN,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
    ),
    _temp("default_setpoint", "default_setpoint", 15.0, 26.0, 0.5, icon="mdi:home-thermometer"),
    _temp("supply_min", "supply_min", 15.0, 45.0, 0.5, icon="mdi:water-thermometer-outline"),
    _temp("supply_max", "supply_max", 20.0, 55.0, 0.5, icon="mdi:water-thermometer"),
    MpcNumberDescription(
        key="max_step",
        field="max_step",
        icon="mdi:stairs-up",
        native_min_value=0.1,
        native_max_value=10.0,
        native_step=0.1,
        native_unit_of_measurement="K/h",
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
    ),
    MpcNumberDescription(
        key="w_move",
        field="w_move",
        icon="mdi:sine-wave",
        native_min_value=0.0,
        native_max_value=50.0,
        native_step=0.1,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
    ),
    MpcNumberDescription(
        key="w_cost",
        field="w_cost",
        icon="mdi:cash-clock",
        native_min_value=0.0,
        native_max_value=20.0,
        native_step=0.05,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: MpcConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add number entities."""
    coordinator = entry.runtime_data
    async_add_entities(MpcNumber(coordinator, desc) for desc in NUMBERS)


class MpcNumber(MpcEntity, RestoreNumber):
    """A controller setting."""

    entity_description: MpcNumberDescription

    def __init__(self, coordinator: MpcCoordinator, description: MpcNumberDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Settings stay editable even when a calculation failed."""
        return True

    @property
    def native_value(self) -> float:
        return float(getattr(self.coordinator.settings, self.entity_description.field))

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            # restore silently, the next scheduled run picks it up
            setattr(self.coordinator.settings, self.entity_description.field, float(last.native_value))

    async def async_set_native_value(self, value: float) -> None:
        setattr(self.coordinator.settings, self.entity_description.field, float(value))
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

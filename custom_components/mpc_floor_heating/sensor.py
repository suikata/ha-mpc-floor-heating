"""Sensors: set point with plan, forecast, status and model diagnostics."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import STATUSES
from .coordinator import MpcConfigEntry, MpcCoordinator
from .entity import MpcEntity

PARALLEL_UPDATES = 0


def _r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(float(value), digits)


def _supply(c: MpcCoordinator) -> float | None:
    res = c.data.result if c.data else None
    return _r(res.supply) if res else None


def _supply_attrs(c: MpcCoordinator) -> dict[str, Any]:
    data = c.data
    if not data or not data.result:
        return {}
    res = data.result
    plan = []
    for k, start in enumerate(data.slot_starts):
        item = {
            "time": start.isoformat(),
            "supply": _r(res.plan_supply[k]),
            "room": _r(res.plan_room[k]),
            "outdoor": _r(data.outdoor_plan[k], 1),
        }
        if data.price_plan and data.price_plan[k] is not None:
            item["price"] = _r(data.price_plan[k], 4)
        plan.append(item)
    return {
        "supply_unlimited": _r(res.supply_raw),
        "steady_state_supply": _r(res.steady_state_supply),
        "written_value": _r(data.written),
        "iterations": res.iterations,
        "plan": plan,
    }


def _room_forecast(c: MpcCoordinator) -> float | None:
    res = c.data.result if c.data else None
    if not res or len(res.plan_room) < 6:
        return None
    return _r(res.plan_room[5])


def _room_forecast_attrs(c: MpcCoordinator) -> dict[str, Any]:
    res = c.data.result if c.data else None
    if not res:
        return {}
    window = res.plan_room[:24]
    return {"min_24h": _r(min(window)), "max_24h": _r(max(window)), "in_12h": _r(res.plan_room[11]) if len(res.plan_room) > 11 else None}


def _model_value(fn: Callable[[Any], Any]) -> Callable[[MpcCoordinator], Any]:
    def inner(c: MpcCoordinator) -> Any:
        return fn(c.model) if c.model else None

    return inner


def _trained(model: Any) -> datetime | None:
    return dt_util.parse_datetime(model.trained_at) if model.trained_at else None


def _model_attrs(c: MpcCoordinator) -> dict[str, Any]:
    m = c.model
    if not m:
        return {"error": c.model_error}
    return {
        "a_floor_to_room": round(m.a, 5),
        "b_room_to_outside": round(m.b, 5),
        "c_gains": round(m.c, 5),
        "rmse_1h": round(m.rmse_1h, 4),
        "rows": m.rows,
        "runner_up": m.extra.get("runner_up"),
        "error": c.model_error,
    }


@dataclass(frozen=True, kw_only=True)
class MpcSensorDescription(SensorEntityDescription):
    """Describes an MPC sensor."""

    value_fn: Callable[[MpcCoordinator], Any]
    attrs_fn: Callable[[MpcCoordinator], dict[str, Any]] | None = None


TEMP = {
    "device_class": SensorDeviceClass.TEMPERATURE,
    "native_unit_of_measurement": UnitOfTemperature.CELSIUS,
    "state_class": SensorStateClass.MEASUREMENT,
    "suggested_display_precision": 1,
}

SENSORS: tuple[MpcSensorDescription, ...] = (
    MpcSensorDescription(key="supply_setpoint", icon="mdi:thermometer-water", value_fn=_supply, attrs_fn=_supply_attrs, **TEMP),
    MpcSensorDescription(
        key="room_average",
        value_fn=lambda c: _r(c.data.room_now) if c.data else None,
        **TEMP,
    ),
    MpcSensorDescription(
        key="room_forecast_6h", icon="mdi:home-thermometer-outline", value_fn=_room_forecast, attrs_fn=_room_forecast_attrs, **TEMP
    ),
    MpcSensorDescription(
        key="status",
        device_class=SensorDeviceClass.ENUM,
        options=STATUSES,
        icon="mdi:state-machine",
        value_fn=lambda c: c.data.status if c.data else None,
        attrs_fn=lambda c: {"message": c.data.message if c.data else None},
    ),
    MpcSensorDescription(
        key="model_dead_time",
        icon="mdi:timer-sand",
        native_unit_of_measurement=UnitOfTime.HOURS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_model_value(lambda m: m.dead_time),
        attrs_fn=_model_attrs,
    ),
    MpcSensorDescription(
        key="model_screed_tau",
        icon="mdi:layers-triple",
        native_unit_of_measurement=UnitOfTime.HOURS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_model_value(lambda m: m.tau),
    ),
    MpcSensorDescription(
        key="model_room_tau",
        icon="mdi:home-clock-outline",
        native_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_model_value(lambda m: round(1.0 / (m.a + m.b), 2)),
    ),
    MpcSensorDescription(
        key="model_error",
        icon="mdi:chart-bell-curve",
        native_unit_of_measurement=UnitOfTemperature.KELVIN,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_model_value(lambda m: round(m.rmse_multi, 3)),
    ),
    MpcSensorDescription(
        key="model_trained",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_model_value(_trained),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: MpcConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add sensors."""
    coordinator = entry.runtime_data
    async_add_entities(MpcSensor(coordinator, desc) for desc in SENSORS)


class MpcSensor(MpcEntity, SensorEntity):
    """Sensor backed by the coordinator."""

    entity_description: MpcSensorDescription
    _unrecorded_attributes = frozenset({"plan", "runner_up"})

    def __init__(self, coordinator: MpcCoordinator, description: MpcSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator)

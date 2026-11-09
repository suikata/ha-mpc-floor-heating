"""Exercise the coordinator against a minimal fake Home Assistant.

This does not replace a test in a real Home Assistant, but it runs the whole
update path (recorder read, training, forecast, MPC, offset write) with
realistic data so wiring errors show up without installing Home Assistant.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import importlib
import sys
import types
from typing import Any, Generic, TypeVar

import numpy as np
import pytest

from conftest_stub import PKG_DIR

_T = TypeVar("_T")
NOW = datetime(2026, 11, 9, 13, 7, tzinfo=UTC)


# --------------------------------------------------------------------- stubs
def _module(name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


class HomeAssistantError(Exception):
    pass


class State:
    def __init__(self, entity_id: str, state: str, attributes: dict | None = None, last_updated: datetime | None = None):
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes or {}
        self.last_updated = last_updated or NOW
        self.last_changed = self.last_updated


class ConfigEntry(Generic[_T]):
    def __init__(self, options: dict):
        self.entry_id = "entry1"
        self.title = "FBH MPC"
        self.data: dict = {}
        self.options = options
        self.unloads: list = []

    def async_on_unload(self, fn):
        self.unloads.append(fn)


class DataUpdateCoordinator(Generic[_T]):
    def __init__(self, hass, logger, *, config_entry=None, name="", update_interval=None):
        self.hass = hass
        self.config_entry = config_entry
        self.data = None

    async def async_refresh(self):
        self.data = await self._async_update_data()

    async def async_request_refresh(self):
        await self.async_refresh()


class UpdateFailed(Exception):
    pass


class Store(Generic[_T]):
    saved: dict = {}

    def __init__(self, hass, version, key):
        self.key = key

    async def async_load(self):
        return Store.saved.get(self.key)

    async def async_save(self, data):
        Store.saved[self.key] = data


def _install_stubs(stats: dict[str, list[dict]]):
    _module("homeassistant")
    _module("homeassistant.components")

    class Recorder:
        async def async_add_executor_job(self, fn, *args):
            return fn(*args)

    def statistics_during_period(hass, start, end, ids, period, units, types_):
        assert period == "hour" and types_ == {"mean"}
        return {
            eid: [r for r in stats.get(eid, []) if start.timestamp() <= r["start"] < end.timestamp()]
            for eid in ids
        }

    _module("homeassistant.components.recorder", get_instance=lambda hass: Recorder())
    _module("homeassistant.components.recorder.history", get_significant_states=lambda *a, **k: {})
    _module("homeassistant.components.recorder.statistics", statistics_during_period=statistics_during_period)
    _module("homeassistant.config_entries", ConfigEntry=ConfigEntry)
    _module("homeassistant.const", ATTR_ENTITY_ID="entity_id", STATE_UNAVAILABLE="unavailable", STATE_UNKNOWN="unknown")
    _module("homeassistant.core", HomeAssistant=object, State=State, callback=lambda f: f)
    _module("homeassistant.exceptions", HomeAssistantError=HomeAssistantError)
    _module("homeassistant.helpers")
    _module("homeassistant.helpers.event", async_track_time_change=lambda *a, **k: (lambda: None))
    _module("homeassistant.helpers.storage", Store=Store)
    _module("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=DataUpdateCoordinator, UpdateFailed=UpdateFailed)
    dt = _module(
        "homeassistant.util.dt",
        now=lambda: NOW,
        utcnow=lambda: NOW,
        parse_datetime=lambda s: datetime.fromisoformat(s),
        get_default_time_zone=lambda: UTC,
    )
    _module("homeassistant.util", dt=dt)


class FakeStates:
    def __init__(self):
        self._s: dict[str, State] = {}

    def set(self, eid, state, attrs=None, last_updated=None):
        self._s[eid] = State(eid, str(state), attrs, last_updated)

    def get(self, eid):
        return self._s.get(eid)


class FakeServices:
    def __init__(self, hass):
        self.hass = hass
        self.calls: list = []

    async def async_call(self, domain, service, data=None, target=None, blocking=False, return_response=False):
        self.calls.append((domain, service, data, target))
        if (domain, service) == ("weather", "get_forecasts"):
            eid = target["entity_id"]
            fc = [
                # naive local times, like Weather Station Core sends them
                {"datetime": (NOW.replace(minute=0, tzinfo=None) + timedelta(hours=h)).isoformat(), "temperature": 4.0 - 0.1 * h}
                for h in range(48)
            ]
            return {eid: {"forecast": fc}}
        return None


class FakeHass:
    def __init__(self):
        self.states = FakeStates()
        self.services = FakeServices(self)

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


# --------------------------------------------------------------------- tests
@pytest.fixture
def setup():
    data = np.load(PKG_DIR.parents[1] / "tests" / "data" / "history.npz")
    ts = data["ts"]
    # shift the recorded window so it ends right before NOW
    shift = (NOW.replace(minute=0).timestamp() - 3600) - ts[-1]
    rooms = ["sensor.room_a", "sensor.room_b"]
    stats: dict[str, list[dict]] = {}

    def rows(arr):
        return [{"start": float(t + shift), "mean": float(v)} for t, v in zip(ts, arr) if np.isfinite(v)]

    stats["sensor.room_a"] = rows(data["room_a"])
    stats["sensor.room_b"] = rows(data["room_b"])
    stats["sensor.supply"] = rows(data["supply"])
    stats["sensor.outdoor"] = rows(data["outdoor"])
    _install_stubs(stats)
    for name in list(sys.modules):
        if name.startswith("mpc_floor_heating."):
            del sys.modules[name]
    if "mpc_floor_heating" not in sys.modules:
        pkg = types.ModuleType("mpc_floor_heating")
        pkg.__path__ = [str(PKG_DIR)]
        sys.modules["mpc_floor_heating"] = pkg
    coord_mod = importlib.import_module("mpc_floor_heating.coordinator")
    const = importlib.import_module("mpc_floor_heating.const")

    hass = FakeHass()
    hass.states.set("sensor.room_a", 21.9)
    hass.states.set("sensor.room_b", 22.3)
    hass.states.set("sensor.supply", 30.4)
    hass.states.set("sensor.outdoor", 5.0)
    hass.states.set("weather.home", "cloudy")
    hass.states.set("number.supply_sp", 30.0, {"min": 20, "max": 45, "step": 0.5}, NOW - timedelta(hours=3))
    options = {
        const.CONF_ROOM_SENSORS: rooms,
        const.CONF_SUPPLY_SENSOR: "sensor.supply",
        const.CONF_OUTDOOR_SENSOR: "sensor.outdoor",
        const.CONF_WEATHER: "weather.home",
        const.CONF_CONTROL_MODE: const.MODE_SENSOR,
        const.CONF_HORIZON: 36,
        const.CONF_TRAIN_DAYS: 30,
    }
    entry = ConfigEntry(options)
    coordinator = coord_mod.MpcCoordinator(hass, entry)
    return hass, coordinator, const


def test_sensor_mode_output_only_when_active(setup):
    hass, coordinator, const = setup

    async def run():
        await coordinator.async_load()
        await coordinator.async_refresh()
        data = coordinator.data
        assert coordinator.model_credible, coordinator.model
        assert coordinator.learning_progress == 100
        assert data.status == const.STATUS_STANDBY, data.message
        assert data.output is None
        assert len(data.result.plan_supply) == 36
        assert 22.0 <= data.result.supply <= 38.0
        assert data.outdoor_plan[0] == pytest.approx(5.0, abs=0.5)  # forecast bias-corrected
        assert data.outdoor_plan[-1] < data.outdoor_plan[0] - 2.0  # the forecast is used, not held

        await coordinator.async_set_active(True)
        data = coordinator.data
        assert data.status == const.STATUS_ACTIVE, data.message
        assert data.output == pytest.approx(round(data.result.supply, 1))
        assert not hass.services.calls or all(c[0] == "weather" for c in hass.services.calls)

        await coordinator.async_set_active(False)
        assert coordinator.data.output is None
        assert coordinator.data.status == const.STATUS_STANDBY

    asyncio.run(run())


def test_number_mode_writes_quantised_value(setup):
    hass, coordinator, const = setup
    coordinator.cfg[const.CONF_CONTROL_MODE] = const.MODE_NUMBER
    coordinator.cfg[const.CONF_TARGET] = "number.supply_sp"

    async def run():
        coordinator.active = True
        await coordinator.async_refresh()
        data = coordinator.data
        assert data.status == const.STATUS_ACTIVE, data.message
        writes = [c for c in hass.services.calls if c[1] == "set_value"]
        assert writes[-1][2]["entity_id"] == "number.supply_sp"
        assert writes[-1][2]["value"] == round(data.result.supply * 2) / 2
        # heartbeat: written again on the next run
        await coordinator.async_refresh()
        assert len([c for c in hass.services.calls if c[1] == "set_value"]) == 2

    asyncio.run(run())


def test_learning_blocks_control(setup):
    hass, coordinator, const = setup

    async def run():
        await coordinator.async_refresh()
        coordinator.model.rows = 50  # pretend we only have two days of data
        coordinator._train_requested = False
        coordinator._async_train = _no_train  # keep the weak model
        coordinator.active = True
        await coordinator.async_refresh()
        assert coordinator.data.status == const.STATUS_LEARNING
        assert coordinator.data.output is None
        assert 0 < coordinator.learning_progress < 100

    asyncio.run(run())


async def _no_train():
    return None


def test_input_missing(setup):
    hass, coordinator, const = setup
    hass.states.set("sensor.room_a", "unavailable")
    hass.states.set("sensor.room_b", "unknown")

    async def run():
        await coordinator.async_refresh()
        assert coordinator.data.status == const.STATUS_INPUT_MISSING

    asyncio.run(run())


def test_enable_entity_pauses(setup):
    hass, coordinator, const = setup
    coordinator.cfg[const.CONF_ENABLE_ENTITY] = "select.mode"
    hass.states.set("select.mode", "Cool")

    async def run():
        coordinator.active = True
        await coordinator.async_refresh()
        assert coordinator.data.status == const.STATUS_PAUSED
        assert not any(c[1] == "set_value" for c in hass.services.calls)

    asyncio.run(run())


def test_number_mode_refuses_offset_like_target(setup):
    hass, coordinator, const = setup
    hass.states.set("number.offset", 0.0, {"min": -5, "max": 10, "step": 1})
    coordinator.cfg[const.CONF_CONTROL_MODE] = const.MODE_NUMBER
    coordinator.cfg[const.CONF_TARGET] = "number.offset"

    async def run():
        coordinator.active = True
        await coordinator.async_refresh()
        assert coordinator.data.status == const.STATUS_WRITE_FAILED
        assert not any(c[1] == "set_value" for c in hass.services.calls)

    asyncio.run(run())

"""Coordinator: collects data, trains the model, runs the MPC, writes the set point."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
import logging
import math
from typing import Any

import numpy as np

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CONTROL_MODE,
    CONF_ENABLE_ENTITY,
    CONF_HORIZON,
    CONF_OUTDOOR_SENSOR,
    CONF_PRICE_SENSOR,
    CONF_ROOM_SENSORS,
    CONF_SUPPLY_SENSOR,
    CONF_TARGET,
    CONF_TRAIN_DAYS,
    CONF_WEATHER,
    CONTROL_MODES,
    DEFAULT_HORIZON,
    DEFAULT_TRAIN_DAYS,
    DISABLED_STATES,
    DOMAIN,
    HISTORY_HOURS,
    MAX_CREDIBLE_ERROR,
    MIN_CREDIBLE_HOURS,
    MODE_CLIMATE,
    MODE_SENSOR,
    RETRAIN_HOUR,
    RETRAIN_MINUTE,
    RUN_MINUTE,
    STATUS_ACTIVE,
    STATUS_INPUT_MISSING,
    STATUS_LEARNING,
    STATUS_PAUSED,
    STATUS_STANDBY,
    STATUS_WRITE_FAILED,
    STORAGE_VERSION,
)
from .model import ModelError, ThermalModel, identify
from .mpc import MpcResult, MpcSettings, Room, solve
from .timeseries import (
    extract_price_points,
    fill_edges,
    hourly_mean_from_points,
    mean_of_rows,
    outdoor_series,
    step_series,
    to_float,
    to_timestamp,
)

_LOGGER = logging.getLogger(__name__)

type MpcConfigEntry = ConfigEntry[MpcCoordinator]


@dataclass
class MpcData:
    """What the entities show."""

    status: str
    result: MpcResult | None = None
    room_now: float | None = None
    rooms: list[dict[str, Any]] = field(default_factory=list)
    shortfall_now: float | None = None  # most negative (room - set point) of active rooms
    outdoor_plan: list[float] = field(default_factory=list)
    price_plan: list[float | None] = field(default_factory=list)
    slot_starts: list[datetime] = field(default_factory=list)
    written: float | None = None
    output: float | None = None  # set point offered by the output sensor
    message: str | None = None


def _state_float(state: State | None) -> float | None:
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    return to_float(state.state)


class MpcCoordinator(DataUpdateCoordinator[MpcData]):
    """Runs the controller once per hour."""

    config_entry: MpcConfigEntry

    def __init__(self, hass: HomeAssistant, entry: MpcConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=None,  # scheduled by the clock, see async_start
        )
        self.cfg: dict[str, Any] = {**entry.data, **entry.options}
        if self.cfg.get(CONF_CONTROL_MODE) not in CONTROL_MODES:  # never write blindly
            self.cfg[CONF_CONTROL_MODE] = MODE_SENSOR
        self.settings = MpcSettings(horizon=int(self.cfg.get(CONF_HORIZON, DEFAULT_HORIZON)))
        self.model: ThermalModel | None = None
        self.model_error: str | None = None
        self.active = False
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._warm_start: np.ndarray | None = None
        self._last_written: float | None = None
        self._train_requested = False
        # room entity -> entity whose statistics describe its temperature
        self._room_history: dict[str, str | None] = {}
        self._resolve_rooms()

    # ------------------------------------------------------------------ rooms
    @callback
    def _resolve_rooms(self) -> None:
        """Map thermostats to their temperature sensors, drop duplicates.

        A climate entity has no long-term statistics of its room temperature,
        so training uses the temperature sensor of the same device. A sensor
        that belongs to a device whose thermostat is selected as well is
        skipped, so the room is not counted twice.
        """
        ent_reg = er.async_get(self.hass)
        selected = list(dict.fromkeys(self.cfg[CONF_ROOM_SENSORS]))
        climate_devices = {
            entry.device_id
            for eid in selected
            if eid.startswith("climate.") and (entry := ent_reg.async_get(eid)) and entry.device_id
        }
        mapping: dict[str, str | None] = {}
        for eid in selected:
            entry = ent_reg.async_get(eid)
            if eid.startswith("climate."):
                mapping[eid] = self._device_temperature_sensor(ent_reg, entry)
                if mapping[eid] is None:
                    _LOGGER.warning("No temperature sensor found on the device of %s, it is not used for training", eid)
            elif entry is not None and entry.device_id in climate_devices:
                continue
            else:
                mapping[eid] = eid
        self._room_history = mapping

    @staticmethod
    def _device_temperature_sensor(ent_reg: er.EntityRegistry, entry: er.RegistryEntry | None) -> str | None:
        if entry is None or entry.device_id is None:
            return None
        skip = ("taupunkt", "dew", "frost", "feels", "gefühlt", "gefuhlt", "apparent", "spreizung", "soll", "target")
        found = []
        for ent in er.async_entries_for_device(ent_reg, entry.device_id):
            if ent.domain != "sensor":
                continue
            if (ent.device_class or ent.original_device_class) != "temperature":
                continue
            text = f"{ent.entity_id} {ent.original_name or ''} {ent.name or ''}".lower()
            if any(word in text for word in skip):
                continue
            found.append(ent.entity_id)
        found.sort(key=lambda e: ("temperat" not in e, e))
        return found[0] if found else None

    def room_states(self) -> list[dict[str, Any]]:
        """Current temperature and set point of every room."""
        out = []
        for eid in self._room_history:
            state = self.hass.states.get(eid)
            name = state.name if state else eid
            if eid.startswith("climate."):
                temp = to_float(state.attributes.get("current_temperature")) if state else None
                setpoint = to_float(state.attributes.get("temperature")) if state else None
                active = (
                    state is not None
                    and state.state not in ("off", STATE_UNAVAILABLE, STATE_UNKNOWN)
                    and setpoint is not None
                    and setpoint >= 10.0
                )
            else:
                temp = _state_float(state)
                setpoint = float(self.settings.default_setpoint)
                active = True
            out.append(
                {
                    "entity_id": eid,
                    "name": name,
                    "temperature": temp,
                    "setpoint": setpoint,
                    "deviation": None if temp is None or setpoint is None else round(temp - setpoint, 2),
                    "active": bool(active and temp is not None),
                    "in_model": self._room_history.get(eid) is not None,
                }
            )
        return out

    # ------------------------------------------------------------------ setup
    async def async_load(self) -> None:
        """Restore the stored model."""
        data = await self._store.async_load()
        if data and data.get("inputs") != self._inputs_fingerprint():
            _LOGGER.info("Input sensors changed, the model will be trained again")
            return
        if data and (model := data.get("model")):
            try:
                self.model = ThermalModel.from_dict(model)
            except (TypeError, ValueError) as err:
                _LOGGER.warning("Stored model ignored: %s", err)

    @callback
    def async_start(self) -> None:
        """Hook the hourly run and the nightly retraining to the clock."""
        entry = self.config_entry
        entry.async_on_unload(
            async_track_time_change(self.hass, self._async_tick, minute=RUN_MINUTE, second=0)
        )
        entry.async_on_unload(
            async_track_time_change(
                self.hass, self._async_retrain_tick, hour=RETRAIN_HOUR, minute=RETRAIN_MINUTE, second=0
            )
        )

    def _inputs_fingerprint(self) -> list[Any]:
        return [
            sorted(e for e in self._room_history.values() if e),
            self.cfg[CONF_SUPPLY_SENSOR],
            self.cfg[CONF_OUTDOOR_SENSOR],
        ]

    @property
    def model_credible(self) -> bool:
        """Enough data and a small enough prediction error to control with."""
        m = self.model
        return m is not None and m.rows >= MIN_CREDIBLE_HOURS and m.rmse_multi <= MAX_CREDIBLE_ERROR

    @property
    def learning_progress(self) -> int:
        """0..100 % of the data needed for a trustworthy model."""
        if self.model is None:
            return 0
        pct = int(min(100.0, 100.0 * self.model.rows / MIN_CREDIBLE_HOURS))
        if pct == 100 and not self.model_credible:
            return 99  # enough data, but the error is still too large
        return pct

    async def _async_tick(self, _now: datetime) -> None:
        await self.async_refresh()

    async def _async_retrain_tick(self, _now: datetime) -> None:
        await self.async_retrain()

    async def async_retrain(self) -> None:
        """Retrain the model now and recompute."""
        self._train_requested = True
        await self.async_refresh()

    # ------------------------------------------------------------- recorder IO
    async def _async_hourly(self, entity_ids: list[str], start: datetime, hours: int) -> dict[str, np.ndarray]:
        """Hourly means from long term statistics, history as a fallback."""
        end = start + timedelta(hours=hours)
        recorder = get_instance(self.hass)
        stats = await recorder.async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            end,
            set(entity_ids),
            "hour",
            None,
            {"mean"},
        )
        start_ts = start.timestamp()
        out: dict[str, np.ndarray] = {}
        for eid in entity_ids:
            arr = np.full(hours, np.nan)
            for row in stats.get(eid, []):
                ts = to_timestamp(row.get("start"))
                mean = to_float(row.get("mean"))
                if ts is None or mean is None:
                    continue
                i = int(round((ts - start_ts) / 3600.0))
                if 0 <= i < hours:
                    arr[i] = mean
            out[eid] = arr

        missing = [eid for eid in entity_ids if not np.isfinite(out[eid]).any()]
        if missing:
            _LOGGER.debug("No statistics for %s, reading state history instead", missing)
            states = await recorder.async_add_executor_job(
                partial(
                    get_significant_states,
                    self.hass,
                    start,
                    end,
                    entity_ids=missing,
                    include_start_time_state=True,
                    significant_changes_only=False,
                    minimal_response=False,
                    no_attributes=True,
                )
            )
            for eid in missing:
                points = []
                for st in states.get(eid, []):
                    val = _state_float(st)
                    points.append((st.last_changed.timestamp(), math.nan if val is None else val))
                out[eid] = hourly_mean_from_points(points, start_ts, hours)
        return out

    async def _async_dataset(self, start: datetime, hours: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Room mean, supply and outdoor hourly series."""
        rooms = sorted({e for e in self._room_history.values() if e})
        supply = self.cfg[CONF_SUPPLY_SENSOR]
        outdoor = self.cfg[CONF_OUTDOOR_SENSOR]
        data = await self._async_hourly([*rooms, supply, outdoor], start, hours)
        room = mean_of_rows([data[r] for r in rooms])
        return room, data[supply], data[outdoor]

    # ----------------------------------------------------------------- training
    async def _async_train(self) -> None:
        days = int(self.cfg.get(CONF_TRAIN_DAYS, DEFAULT_TRAIN_DAYS))
        hour0 = dt_util.now().replace(minute=0, second=0, microsecond=0)
        start = hour0 - timedelta(days=days)
        y, u, o = await self._async_dataset(start, days * 24)
        try:
            model = await self.hass.async_add_executor_job(identify, y, u, o)
        except ModelError as err:
            self.model_error = "model_not_identifiable"
            _LOGGER.warning("Model identification failed: %s", err)
            return
        model.trained_at = dt_util.utcnow().isoformat()
        self.model = model
        self.model_error = None
        self._warm_start = None
        await self._store.async_save({"model": model.as_dict(), "inputs": self._inputs_fingerprint()})
        _LOGGER.info(
            "Model trained on %s hours: dead time %s h, screed tau %.0f h, room tau %.1f h, rmse %.3f K",
            model.rows,
            model.dead_time,
            model.tau,
            1.0 / (model.a + model.b),
            model.rmse_multi,
        )

    # --------------------------------------------------------------- forecasts
    async def _async_outdoor_forecast(self) -> list[tuple[float, float]]:
        weather = self.cfg.get(CONF_WEATHER)
        if not weather:
            return []
        try:
            resp = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"type": "hourly"},
                target={ATTR_ENTITY_ID: weather},
                blocking=True,
                return_response=True,
            )
        except HomeAssistantError as err:
            _LOGGER.debug("Hourly forecast of %s not available: %s", weather, err)
            return []
        points = []
        for item in (resp or {}).get(weather, {}).get("forecast", []):
            ts = to_timestamp(item.get("datetime"), dt_util.parse_datetime, dt_util.get_default_time_zone())
            temp = to_float(item.get("temperature"))
            if ts is not None and temp is not None:
                points.append((ts, temp))
        return points

    def _price_points(self) -> list[tuple[float, float]]:
        sensor = self.cfg.get(CONF_PRICE_SENSOR)
        if not sensor or (state := self.hass.states.get(sensor)) is None:
            return []
        return extract_price_points(
            dict(state.attributes), dt_util.parse_datetime, dt_util.get_default_time_zone()
        )

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _room_mean(rooms: list[dict[str, Any]]) -> float | None:
        """Mean over the rooms the model was trained on (same set as history)."""
        vals = [r["temperature"] for r in rooms if r["in_model"] and r["temperature"] is not None]
        return float(np.mean(vals)) if vals else None

    def _control_allowed(self) -> bool:
        enable = self.cfg.get(CONF_ENABLE_ENTITY)
        if enable:
            state = self.hass.states.get(enable)
            if state is None or state.state.lower() in DISABLED_STATES:
                return False
        if self.cfg.get(CONF_CONTROL_MODE) == MODE_CLIMATE:
            state = self.hass.states.get(self.cfg[CONF_TARGET])
            if state is None or state.state in ("off", STATE_UNAVAILABLE, STATE_UNKNOWN):
                return False
        return True

    def current_setpoint(self) -> float | None:
        """Supply set point that is in effect right now."""
        mode = self.cfg.get(CONF_CONTROL_MODE, MODE_SENSOR)
        if mode == MODE_SENSOR:
            return self._last_written if self.active else None
        state = self.hass.states.get(self.cfg[CONF_TARGET])
        if state is None:
            return None
        value = to_float(state.attributes.get("temperature")) if mode == MODE_CLIMATE else _state_float(state)
        if value is None or not 10.0 <= value <= 80.0:  # not a supply temperature
            return None
        return value

    # ------------------------------------------------------------------- update
    async def _async_update_data(self) -> MpcData:
        if self.model is None or self._train_requested or not self.model_credible:
            self._train_requested = False
            try:
                await self._async_train()
            except HomeAssistantError as err:
                raise UpdateFailed(f"Reading the recorder failed: {err}") from err
        if self.model is None:
            return MpcData(status=STATUS_LEARNING, message=self.model_error or "collecting_data")

        model = self.model
        horizon = int(self.settings.horizon)
        now = dt_util.now()
        hour0 = now.replace(minute=0, second=0, microsecond=0)
        hist_hours = max(HISTORY_HOURS, model.dead_time + int(6 * model.tau) + 2)
        try:
            _y, u_hist, _o = await self._async_dataset(hour0 - timedelta(hours=hist_hours), hist_hours)
        except HomeAssistantError as err:
            raise UpdateFailed(f"Reading the recorder failed: {err}") from err

        rooms = self.room_states()
        room_now = self._room_mean(rooms)
        active = [r for r in rooms if r["active"]]
        mpc_rooms = (
            [Room(r["temperature"] - room_now, r["setpoint"]) for r in active] if room_now is not None else []
        )
        shortfall = min((r["deviation"] for r in active), default=None)
        supply_now = _state_float(self.hass.states.get(self.cfg[CONF_SUPPLY_SENSOR]))
        outdoor_now = _state_float(self.hass.states.get(self.cfg[CONF_OUTDOOR_SENSOR]))
        if room_now is None or (not np.isfinite(u_hist).any() and supply_now is None):
            return MpcData(status=STATUS_INPUT_MISSING, message="room_or_supply_missing")
        u_hist = fill_edges(u_hist, supply_now)

        slot_starts = [hour0 + timedelta(hours=k) for k in range(horizon)]
        slot_ts = np.array([s.timestamp() for s in slot_starts])
        try:
            outdoor = outdoor_series(await self._async_outdoor_forecast(), slot_ts, now.timestamp(), outdoor_now)
        except ValueError:
            return MpcData(status=STATUS_INPUT_MISSING, message="outdoor_missing")
        price_pts = self._price_points()
        price = step_series(price_pts, slot_ts) if price_pts else None

        u_prev = self.current_setpoint()
        if u_prev is None:
            u_prev = self._last_written
        try:
            result = await self.hass.async_add_executor_job(
                partial(
                    solve,
                    model,
                    self.settings,
                    room_now,
                    u_hist,
                    outdoor,
                    price,
                    u_prev,
                    self._warm_start,
                    mpc_rooms or None,
                )
            )
        except (ValueError, np.linalg.LinAlgError) as err:
            raise UpdateFailed(f"Optimisation failed: {err}") from err
        free = max(1, horizon - model.dead_time - 1)
        self._warm_start = np.array(result.plan_supply[1 : free + 1] + [result.plan_supply[-1]])

        data = MpcData(
            status=STATUS_STANDBY,
            result=result,
            room_now=room_now,
            rooms=rooms,
            shortfall_now=shortfall,
            outdoor_plan=[float(x) for x in outdoor],
            price_plan=(
                [None] * horizon
                if price is None
                else [float(p) if np.isfinite(p) else None for p in price]
            ),
            slot_starts=slot_starts,
        )
        if not self.model_credible:
            data.status = STATUS_LEARNING
            data.message = "collecting_data"
            return data
        if not self.active:
            return data
        if not self._control_allowed():
            data.status = STATUS_PAUSED
            return data
        if self.cfg.get(CONF_CONTROL_MODE, MODE_SENSOR) == MODE_SENSOR:
            data.output = round(result.supply, 1)
            self._last_written = data.output
            data.status = STATUS_ACTIVE
            return data
        try:
            data.written = await self._async_write(result.supply)
            data.status = STATUS_ACTIVE
        except HomeAssistantError as err:
            _LOGGER.error("Writing the set point failed: %s", err)
            data.status = STATUS_WRITE_FAILED
            data.message = "write_error"
        return data

    # -------------------------------------------------------------------- write
    @staticmethod
    def _quantize(value: float, step: float | None, low: float | None, high: float | None) -> float:
        if step and step > 0:
            value = round(value / step) * step
        if low is not None:
            value = max(low, value)
        if high is not None:
            value = min(high, value)
        return round(value, 3)

    async def _async_write(self, supply: float) -> float | None:
        """Send the set point to the target entity. Returns the written value."""
        mode = self.cfg[CONF_CONTROL_MODE]
        target = self.cfg[CONF_TARGET]
        state = self.hass.states.get(target)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            raise HomeAssistantError(f"{target} is not available")
        attrs = state.attributes

        if mode == MODE_CLIMATE:
            value = self._quantize(
                supply,
                to_float(attrs.get("target_temp_step")) or 0.1,
                to_float(attrs.get("min_temp")),
                to_float(attrs.get("max_temp")),
            )
            await self.hass.services.async_call(
                "climate", "set_temperature", {ATTR_ENTITY_ID: target, "temperature": value}, blocking=True
            )
            self._last_written = value
            return value

        low, high = to_float(attrs.get("min")), to_float(attrs.get("max"))
        if high is not None and high < self.settings.supply_min:
            # e.g. an offset number was chosen: never push a supply temperature into it
            raise HomeAssistantError(
                f"{target} accepts at most {high}, which cannot be a supply temperature"
            )
        value = self._quantize(supply, to_float(attrs.get("step")), low, high)
        # written every run, even when unchanged: acts as a heartbeat for a
        # watchdog in the heating controller
        await self.hass.services.async_call(
            target.split(".", 1)[0], "set_value", {ATTR_ENTITY_ID: target, "value": value}, blocking=True
        )
        self._last_written = value
        return value

    async def async_set_active(self, active: bool) -> None:
        """Switch control on or off and recompute right away."""
        self.active = active
        if not active:
            self._last_written = None
        await self.async_request_refresh()

"""Constants for MPC Floor Heating."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "mpc_floor_heating"

# --- config entry keys -------------------------------------------------------
CONF_ROOM_SENSORS: Final = "room_sensors"
CONF_OUTDOOR_SENSOR: Final = "outdoor_sensor"
CONF_SUPPLY_SENSOR: Final = "supply_sensor"
CONF_WEATHER: Final = "weather_entity"
CONF_PRICE_SENSOR: Final = "price_sensor"
CONF_CONTROL_MODE: Final = "control_mode"
CONF_TARGET: Final = "target_entity"
CONF_SETPOINT_SENSOR: Final = "setpoint_sensor"
CONF_ENABLE_ENTITY: Final = "enable_entity"
CONF_HORIZON: Final = "horizon"
CONF_TRAIN_DAYS: Final = "train_days"
CONF_OFFSET_RESET: Final = "offset_reset"

MODE_OFFSET: Final = "offset"
MODE_CLIMATE: Final = "climate"
MODE_ABSOLUTE: Final = "absolute"
CONTROL_MODES: Final = [MODE_OFFSET, MODE_CLIMATE, MODE_ABSOLUTE]

DEFAULT_NAME: Final = "FBH MPC"
DEFAULT_HORIZON: Final = 36
DEFAULT_TRAIN_DAYS: Final = 30
DEFAULT_OFFSET_RESET: Final = 0.0

# --- scheduling ----------------------------------------------------------------
RUN_MINUTE: Final = 7  # every hour at hh:07, after HA compiled the hourly statistics
RETRAIN_HOUR: Final = 3
RETRAIN_MINUTE: Final = 17
HISTORY_HOURS: Final = 96  # supply history fed into the floor state filter

# --- status values -------------------------------------------------------------
STATUS_NO_MODEL: Final = "no_model"
STATUS_SHADOW: Final = "shadow"
STATUS_ACTIVE: Final = "active"
STATUS_PAUSED: Final = "paused"
STATUS_INPUT_MISSING: Final = "input_missing"
STATUS_WRITE_FAILED: Final = "write_failed"
STATUSES: Final = [
    STATUS_NO_MODEL,
    STATUS_SHADOW,
    STATUS_ACTIVE,
    STATUS_PAUSED,
    STATUS_INPUT_MISSING,
    STATUS_WRITE_FAILED,
]

# states of the optional enable entity that mean "do not control"
DISABLED_STATES: Final = {"off", "unavailable", "unknown", "none", "", "false", "cool"}

STORAGE_VERSION: Final = 1

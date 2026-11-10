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
CONF_ENABLE_ENTITY: Final = "enable_entity"
CONF_HORIZON: Final = "horizon"
CONF_TRAIN_DAYS: Final = "train_days"

# sensor: the integration provides the set point as its own sensor, the heating
#         controller reads it (e.g. ESPHome "homeassistant" sensor)
# climate / number: the set point is written to an existing entity
MODE_SENSOR: Final = "sensor"
MODE_CLIMATE: Final = "climate"
MODE_NUMBER: Final = "number"
CONTROL_MODES: Final = [MODE_SENSOR, MODE_CLIMATE, MODE_NUMBER]

DEFAULT_NAME: Final = "FBH MPC"
DEFAULT_HORIZON: Final = 36
DEFAULT_TRAIN_DAYS: Final = 30

# learning: a model is only used for control once it is trustworthy
MIN_CREDIBLE_HOURS: Final = 7 * 24  # usable hours of data
MAX_CREDIBLE_ERROR: Final = 0.5  # K, 12 h open-loop RMSE

# --- scheduling ----------------------------------------------------------------
RUN_MINUTE: Final = 7  # every hour at hh:07, after HA compiled the hourly statistics
RETRAIN_HOUR: Final = 3
RETRAIN_MINUTE: Final = 17
HISTORY_HOURS: Final = 96  # supply history fed into the floor state filter
SMOOTH_HOURS: Final = 2  # room temperatures are averaged over this window (ventilating, door open)
STARTUP_HOLD_SECONDS: Final = 180  # after a restart wait for thermostats and sensors to report

# --- status values -------------------------------------------------------------
STATUS_LEARNING: Final = "learning"
STATUS_STANDBY: Final = "standby"
STATUS_ACTIVE: Final = "active"
STATUS_PAUSED: Final = "paused"
STATUS_INPUT_MISSING: Final = "input_missing"
STATUS_WRITE_FAILED: Final = "write_failed"
STATUSES: Final = [
    STATUS_LEARNING,
    STATUS_STANDBY,
    STATUS_ACTIVE,
    STATUS_PAUSED,
    STATUS_INPUT_MISSING,
    STATUS_WRITE_FAILED,
]

# states of the optional enable entity that mean "do not control"
DISABLED_STATES: Final = {"off", "unavailable", "unknown", "none", "", "false", "cool"}

STORAGE_VERSION: Final = 1

# heuristics for preselecting indoor room temperature sensors (token based:
# short words must match a whole token, words with 5+ letters also as prefix)
OUTDOOR_WORDS: Final = (
    "aussen", "außen", "outdoor", "outside", "exterior", "garten", "garden", "terrasse",
    "terrace", "balkon", "balcony", "garage", "carport", "wetter", "weather", "front", "soil",
)
TECHNICAL_WORDS: Final = (
    # heating hydraulics
    "vorlauf", "rücklauf", "rucklauf", "flow", "return", "supply", "sole", "brine", "buffer",
    "puffer", "speicher", "boiler", "brauchwasser", "water", "wasser", "ofen", "stove",
    "heizung", "heating",
    # derived climate values
    "taupunkt", "dew", "frost", "apparent", "gefühlt", "gefuhlte", "feels", "humidex", "chill",
    "windchill", "wind", "index", "utci", "wbgt", "hitzeindex", "feuchtkulb", "stress", "spreizung",
    # device internals
    "chip", "cpu", "board", "zone", "soc", "esp", "internal", "interne", "intern", "processor",
    "device", "gerät", "geräte", "geratetemperatur", "gerätetemperatur", "nozzle", "düse", "duse",
    "bed", "druckbett", "printer", "fridge", "freezer", "kühl", "gefrier", "battery", "akku",
    "zelle", "zelltemperatur", "body", "basal", "liquid", "steckdose", "schaltsteckdose", "plug",
    "licht", "light", "bewegungsmelder", "motion", "vibr", "relais", "relay", "taster", "button",
    "echo", "switch", "poe", "miner", "ac", "ams",
    # statistics, targets and helpers
    "mittel", "mean", "median", "average", "durchschnitt", "temperaturdurchschnitt", "min", "max",
    "minimum", "maximum", "high", "low", "höchstwert", "hochstwert", "tiefstwert", "trend",
    "⌀", "∅", "1d", "7d", "24h", "12h", "6h", "year", "week", "month", "target", "ziel",
    "zieltemperatur", "soll", "solltemperatur", "setpoint", "mpc",
    # weather services
    "openweathermap", "ecowitt", "ws90",
)
TECHNICAL_AREA_WORDS: Final = (
    "heizraum", "technik", "technikraum", "server", "serverraum", "kontrollraum", "schaltschrank",
)

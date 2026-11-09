"""Config and options flow."""

from __future__ import annotations

import re
from typing import Any

try:  # Home Assistant replaced voluptuous with the compatible probatio
    import probatio as vol
except ImportError:  # pragma: no cover - older Home Assistant releases
    import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

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
    DEFAULT_NAME,
    DEFAULT_TRAIN_DAYS,
    DOMAIN,
    MODE_CLIMATE,
    MODE_SENSOR,
    OUTDOOR_WORDS,
    TECHNICAL_AREA_WORDS,
    TECHNICAL_WORDS,
)

TEMP_UNITS = {"°C", "°F", "K"}

TEMP_SENSOR = EntitySelector(EntitySelectorConfig(domain="sensor", device_class="temperature"))
MODE_SELECTOR = SelectSelector(
    SelectSelectorConfig(options=CONTROL_MODES, mode=SelectSelectorMode.LIST, translation_key=CONF_CONTROL_MODE)
)


# ------------------------------------------------------------ indoor sensors
def _tokens(*texts: str | None) -> list[str]:
    out: list[str] = []
    for text in texts:
        if text:
            out.extend(t for t in re.split(r"[^0-9a-zäöüß⌀∅]+", text.lower()) if t)
    return out


def _matches(tokens: list[str], words: tuple[str, ...]) -> bool:
    """Whole tokens for short words, prefixes for long ones (vorlauf -> vorlauftemperatur)."""
    for tok in tokens:
        for word in words:
            if tok == word or (len(word) >= 5 and tok.startswith(word)):
                return True
    return False


def indoor_temperature_sensors(hass: HomeAssistant) -> list[str]:
    """Temperature sensors that look like room thermometers.

    Excludes everything whose id, name or area hints at outdoor or technical
    use (supply, buffer, dew point, CPU, statistics helpers, ...).
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)
    found: list[str] = []
    for state in hass.states.async_all("sensor"):
        attrs = state.attributes
        if attrs.get("device_class") != "temperature" or attrs.get("unit_of_measurement") not in TEMP_UNITS:
            continue
        entry = ent_reg.async_get(state.entity_id)
        if entry is not None and (entry.platform == DOMAIN or entry.disabled_by or entry.hidden_by):
            continue
        area_id = entry.area_id if entry else None
        if area_id is None and entry is not None and entry.device_id:
            device = dev_reg.async_get(entry.device_id)
            area_id = device.area_id if device else None
        area = area_reg.async_get_area(area_id) if area_id else None
        tokens = _tokens(state.entity_id, attrs.get("friendly_name"))
        area_tokens = _tokens(area.name if area else None)
        if (
            _matches(tokens + area_tokens, OUTDOOR_WORDS)
            or _matches(tokens, TECHNICAL_WORDS)
            or _matches(area_tokens, TECHNICAL_AREA_WORDS)
        ):
            continue
        found.append(state.entity_id)
    return sorted(found)


def _room_selector(candidates: list[str]) -> EntitySelector:
    config = EntitySelectorConfig(domain="sensor", device_class="temperature", multiple=True)
    if candidates:
        config["include_entities"] = candidates
    return EntitySelector(config)


# ------------------------------------------------------------------- schemas
def _sensor_schema(candidates: list[str]) -> dict[Any, Any]:
    return {
        vol.Required(CONF_ROOM_SENSORS): _room_selector(candidates),
        vol.Required(CONF_SUPPLY_SENSOR): TEMP_SENSOR,
        vol.Required(CONF_OUTDOOR_SENSOR): TEMP_SENSOR,
        vol.Optional(CONF_WEATHER): EntitySelector(
            EntitySelectorConfig(
                filter={"domain": "weather", "supported_features": ["weather.WeatherEntityFeature.FORECAST_HOURLY"]}
            )
        ),
        vol.Optional(CONF_PRICE_SENSOR): EntitySelector(EntitySelectorConfig(domain="sensor")),
    }


def _target_schema(mode: str) -> dict[Any, Any]:
    if mode == MODE_CLIMATE:
        selector = EntitySelector(EntitySelectorConfig(domain="climate"))
    else:
        selector = EntitySelector(EntitySelectorConfig(domain=["number", "input_number"]))
    return {vol.Required(CONF_TARGET): selector}


def _advanced_schema() -> dict[Any, Any]:
    return {
        vol.Optional(CONF_ENABLE_ENTITY): EntitySelector(EntitySelectorConfig()),
        vol.Required(CONF_HORIZON, default=DEFAULT_HORIZON): NumberSelector(
            NumberSelectorConfig(min=12, max=72, step=1, unit_of_measurement="h", mode=NumberSelectorMode.BOX)
        ),
        vol.Required(CONF_TRAIN_DAYS, default=DEFAULT_TRAIN_DAYS): NumberSelector(
            NumberSelectorConfig(min=7, max=120, step=1, unit_of_measurement="d", mode=NumberSelectorMode.BOX)
        ),
    }


def _normalise(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    for key in (CONF_HORIZON, CONF_TRAIN_DAYS):
        if key in out:
            out[key] = int(out[key])
    if out.get(CONF_CONTROL_MODE) == MODE_SENSOR:
        out.pop(CONF_TARGET, None)
    return out




class MpcConfigFlow(ConfigFlow, domain=DOMAIN):
    """Setup: measurements -> control mode -> (target entity)."""

    VERSION = 2

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        candidates = indoor_temperature_sensors(self.hass)
        if user_input is not None:
            if not user_input.get(CONF_ROOM_SENSORS):
                errors[CONF_ROOM_SENSORS] = "rooms_required"
            else:
                self._data = dict(user_input)
                return await self.async_step_control()
        schema = vol.Schema({vol.Required(CONF_NAME, default=DEFAULT_NAME): TextSelector(), **_sensor_schema(candidates)})
        suggested = user_input if user_input is not None else {CONF_ROOM_SENSORS: candidates}
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
            
        )

    async def async_step_control(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            if user_input[CONF_CONTROL_MODE] == MODE_SENSOR:
                return self._finish()
            return await self.async_step_target()
        schema = vol.Schema({vol.Required(CONF_CONTROL_MODE, default=MODE_SENSOR): MODE_SELECTOR})
        return self.async_show_form(step_id="control", data_schema=schema)

    async def async_step_target(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        mode = self._data[CONF_CONTROL_MODE]
        if user_input is not None:
            self._data.update(user_input)
            return self._finish()
        return self.async_show_form(
            step_id="target_climate" if mode == MODE_CLIMATE else "target_number",
            data_schema=vol.Schema(_target_schema(mode)),
        )

    # both target forms post back to async_step_target
    async_step_target_climate = async_step_target
    async_step_target_number = async_step_target

    def _finish(self) -> ConfigFlowResult:
        data = dict(self._data)
        title = data.pop(CONF_NAME, DEFAULT_NAME)
        options = _normalise({CONF_HORIZON: DEFAULT_HORIZON, CONF_TRAIN_DAYS: DEFAULT_TRAIN_DAYS, **data})
        return self.async_create_entry(title=title, data={}, options=options)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return MpcOptionsFlow()


class MpcOptionsFlow(OptionsFlow):
    """Change every setting except the name; same steps as the setup."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        candidates = indoor_temperature_sensors(self.hass)
        current = dict(self.config_entry.options)
        # keep sensors chosen earlier selectable even if the heuristic skips them
        candidates = sorted(set(candidates) | set(current.get(CONF_ROOM_SENSORS, [])))
        if user_input is not None:
            if not user_input.get(CONF_ROOM_SENSORS):
                errors[CONF_ROOM_SENSORS] = "rooms_required"
            else:
                self._data = {CONF_TARGET: current.get(CONF_TARGET), **user_input}
                if user_input[CONF_CONTROL_MODE] == MODE_SENSOR:
                    return self.async_create_entry(data=_normalise(self._data))
                return await self.async_step_target()
        schema = vol.Schema(
            {
                **_sensor_schema(candidates),
                vol.Required(CONF_CONTROL_MODE, default=MODE_SENSOR): MODE_SELECTOR,
                **_advanced_schema(),
            }
        )
        if current.get(CONF_CONTROL_MODE) not in CONTROL_MODES:
            current[CONF_CONTROL_MODE] = MODE_SENSOR
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or current),
            errors=errors,
            
        )

    async def async_step_target(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        mode = self._data[CONF_CONTROL_MODE]
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(data=_normalise(self._data))
        schema = vol.Schema(_target_schema(mode))
        target = self._data.get(CONF_TARGET)
        domain = str(target or "").split(".", 1)[0]
        fits = domain == "climate" if mode == MODE_CLIMATE else domain in ("number", "input_number")
        return self.async_show_form(
            step_id="target_climate" if mode == MODE_CLIMATE else "target_number",
            data_schema=self.add_suggested_values_to_schema(schema, {CONF_TARGET: target} if fits else {}),
        )

    async_step_target_climate = async_step_target
    async_step_target_number = async_step_target

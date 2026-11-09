"""Config and options flow."""

from __future__ import annotations

from typing import Any

try:  # Home Assistant replaced voluptuous with the compatible probatio
    import probatio as vol
except ImportError:  # pragma: no cover - older Home Assistant releases
    import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
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
    CONF_OFFSET_RESET,
    CONF_OUTDOOR_SENSOR,
    CONF_PRICE_SENSOR,
    CONF_ROOM_SENSORS,
    CONF_SETPOINT_SENSOR,
    CONF_SUPPLY_SENSOR,
    CONF_TARGET,
    CONF_TRAIN_DAYS,
    CONF_WEATHER,
    CONTROL_MODES,
    DEFAULT_HORIZON,
    DEFAULT_NAME,
    DEFAULT_OFFSET_RESET,
    DEFAULT_TRAIN_DAYS,
    DOMAIN,
    MODE_CLIMATE,
    MODE_OFFSET,
)

# no device_class filter: template or ESPHome sensors often lack it
TEMP_SENSOR = EntitySelector(EntitySelectorConfig(domain="sensor"))


def _sensor_fields() -> dict[Any, Any]:
    return {
        vol.Required(CONF_ROOM_SENSORS): EntitySelector(
            EntitySelectorConfig(domain="sensor", multiple=True)
        ),
        vol.Required(CONF_SUPPLY_SENSOR): TEMP_SENSOR,
        vol.Required(CONF_OUTDOOR_SENSOR): TEMP_SENSOR,
        vol.Optional(CONF_WEATHER): EntitySelector(EntitySelectorConfig(domain="weather")),
        vol.Optional(CONF_PRICE_SENSOR): EntitySelector(EntitySelectorConfig(domain="sensor")),
    }


def _control_fields() -> dict[Any, Any]:
    return {
        vol.Required(CONF_CONTROL_MODE, default=MODE_OFFSET): SelectSelector(
            SelectSelectorConfig(
                options=CONTROL_MODES, mode=SelectSelectorMode.LIST, translation_key=CONF_CONTROL_MODE
            )
        ),
        vol.Required(CONF_TARGET): EntitySelector(
            EntitySelectorConfig(domain=["number", "input_number", "climate"])
        ),
        vol.Optional(CONF_SETPOINT_SENSOR): TEMP_SENSOR,
        vol.Optional(CONF_ENABLE_ENTITY): EntitySelector(EntitySelectorConfig()),
    }


def _advanced_fields() -> dict[Any, Any]:
    return {
        vol.Required(CONF_HORIZON, default=DEFAULT_HORIZON): NumberSelector(
            NumberSelectorConfig(min=12, max=72, step=1, unit_of_measurement="h", mode=NumberSelectorMode.BOX)
        ),
        vol.Required(CONF_TRAIN_DAYS, default=DEFAULT_TRAIN_DAYS): NumberSelector(
            NumberSelectorConfig(min=7, max=120, step=1, unit_of_measurement="d", mode=NumberSelectorMode.BOX)
        ),
        vol.Required(CONF_OFFSET_RESET, default=DEFAULT_OFFSET_RESET): NumberSelector(
            NumberSelectorConfig(min=-20, max=20, step=0.1, mode=NumberSelectorMode.BOX)
        ),
    }


def _validate_control(data: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    mode = data.get(CONF_CONTROL_MODE)
    domain = str(data.get(CONF_TARGET, "")).split(".", 1)[0]
    if mode == MODE_CLIMATE and domain != "climate":
        errors[CONF_TARGET] = "target_not_climate"
    elif mode != MODE_CLIMATE and domain not in ("number", "input_number"):
        errors[CONF_TARGET] = "target_not_number"
    if mode == MODE_OFFSET and not data.get(CONF_SETPOINT_SENSOR):
        errors[CONF_SETPOINT_SENSOR] = "setpoint_required"
    return errors


def _normalise(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    for key in (CONF_HORIZON, CONF_TRAIN_DAYS):
        if key in out:
            out[key] = int(out[key])
    if CONF_OFFSET_RESET in out:
        out[CONF_OFFSET_RESET] = float(out[CONF_OFFSET_RESET])
    return out


class MpcConfigFlow(ConfigFlow, domain=DOMAIN):
    """Two step setup: sensors, then the actuator."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_ROOM_SENSORS):
                errors[CONF_ROOM_SENSORS] = "rooms_required"
            else:
                self._data = dict(user_input)
                return await self.async_step_control()
        schema = vol.Schema(
            {vol.Required(CONF_NAME, default=DEFAULT_NAME): TextSelector(), **_sensor_fields()}
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    async def async_step_control(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_control(user_input)
            if not errors:
                data = {**self._data, **user_input}
                title = data.pop(CONF_NAME)
                options = _normalise(
                    {
                        CONF_HORIZON: DEFAULT_HORIZON,
                        CONF_TRAIN_DAYS: DEFAULT_TRAIN_DAYS,
                        CONF_OFFSET_RESET: DEFAULT_OFFSET_RESET,
                        **data,
                    }
                )
                return self.async_create_entry(title=title, data={}, options=options)
        schema = vol.Schema(_control_fields())
        return self.async_show_form(
            step_id="control",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return MpcOptionsFlow()


class MpcOptionsFlow(OptionsFlow):
    """Change every setting except the name after setup."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_control(user_input)
            if not user_input.get(CONF_ROOM_SENSORS):
                errors[CONF_ROOM_SENSORS] = "rooms_required"
            if not errors:
                return self.async_create_entry(data=_normalise(user_input))
        schema = vol.Schema({**_sensor_fields(), **_control_fields(), **_advanced_fields()})
        current = user_input if user_input is not None else dict(self.config_entry.options)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, current),
            errors=errors,
        )

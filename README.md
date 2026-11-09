# MPC Floor Heating for Home Assistant

Model predictive control (MPC) of the **supply (flow) temperature** of a floor heating circuit.

A screed floor reacts to a change of the supply temperature only after hours. A heating curve
only looks at the outdoor temperature right now, so the room overshoots on sunny days and
lags behind on cold fronts. This integration

1. **learns a model of your building** from the recorder statistics you already have
   (room temperatures, measured supply temperature, outdoor temperature),
2. **looks 36 hours ahead** with the hourly weather forecast (and optionally an electricity price
   forecast),
3. **computes the supply temperature plan** that keeps the rooms inside your comfort band with as
   little and as calm heating as possible,
4. and **writes the first hour of that plan** to your heating controller, every hour.

Everything is set up and tuned in the UI. No YAML, no add-on, no cloud.

## How it works

The model has two thermal storages, the screed and the room air:

```
screed:  xf[k+1] = λ·xf[k] + (1-λ)·u[k-d]                λ = exp(-1/τ)
room:    y[k+1]  = y[k] + a·(xf[k]-y[k]) + b·(o[k]-y[k]) + c
```

`u` supply temperature, `o` outdoor temperature, `y` mean room temperature, `d` dead time,
`τ` screed time constant. `d` and `τ` are found by grid search, `a`, `b`, `c` by least squares; the
candidate with the smallest **12-hour open-loop prediction error** wins, because that is what the
controller relies on. The model is retrained every night at 03:17 from the last 30 days.

The controller minimises, over the horizon,

* squared violations of the comfort band (strong weight),
* the distance to the middle of the band (weak weight),
* changes of the supply temperature (“smoothing weight”),
* optionally `price × supply temperature` (“energy / price weight”),

subject to the supply minimum and maximum. It is a convex problem, solved in pure numpy
(accelerated projected gradient) in a few milliseconds.

## Installation

**HACS:** HACS → ⋮ → Custom repositories → add this repository as type *Integration* →
install *MPC Floor Heating* → restart Home Assistant.

**Manual:** copy `custom_components/mpc_floor_heating` into `/config/custom_components/` and restart.

Then: **Settings → Devices & services → Add integration → MPC Floor Heating**.

### Requirements

* Hourly **long-term statistics** for the room, supply and outdoor sensors
  (sensors with `state_class: measurement`; otherwise the state history is used, which usually only
  covers 10 days). At least 3 days of data, better 2 to 4 weeks.
* A measured **supply temperature** of the circuit (after the mixing valve).
* A way to influence the supply set point (see control modes).
* Optional: a `weather` entity with an **hourly** forecast, a price sensor.
* Temperatures in °C.

## Setup

**Step 1, measurements:** room sensors of the circuit (the mean is controlled), measured supply
temperature, outdoor temperature (a median of several sensors is ideal), weather entity, price sensor.

The price sensor needs a list of hourly prices in an attribute. Detected automatically:
EPEX Spot (`data`: `start_time`/`price_per_kwh`), Nord Pool (`raw_today`/`raw_tomorrow`:
`start`/`value`), Tibber-style (`startsAt`/`total`) and similar.

**Step 2, actuator:**

| Control mode | Target entity | When to use |
|---|---|---|
| **Offset** (recommended) | the `number` that shifts your heating curve | Your controller has a heating curve with an offset. The MPC writes `offset = MPC set point − (effective set point − current offset)`. If Home Assistant stops, the curve keeps running with the last offset. Needs the *effective set point sensor* (curve + offset). |
| **Absolute (number)** | `number` / `input_number` | The controller takes a supply set point directly. |
| **Climate** | `climate` entity of the supply controller | A climate entity whose target temperature is the supply set point. |

*Enable entity* (optional): control only while this entity is `on` / `heat` (e.g. an operating mode
select). Anything `off`, `cool`, `unavailable` pauses the controller.

After setup the integration runs in **shadow mode**: it calculates and shows the plan but writes
nothing. Compare its set point with your heating curve for a few days, then turn on
**Control active**.

## Entities

| Entity | Meaning |
|---|---|
| Supply set point | Set point for the current hour. Attribute `plan`: hourly plan of supply, predicted room temperature, outdoor and price for the whole horizon. |
| Room temperature mean | Mean of the room sensors right now. |
| Room forecast in 6 h | Predicted room temperature (attributes: in 12 h, min/max next 24 h). |
| Status | `shadow`, `active`, `paused`, `no_model`, `input_missing`, `write_failed`. |
| Comfort minimum / maximum | Comfort band of the room mean. |
| Supply minimum / maximum | Limits of the supply set point. |
| Max change per hour | Rate limit of the written set point. |
| Smoothing weight | Higher = calmer supply temperature, slower reaction. |
| Energy / price weight | 0 = comfort only. Higher = use the comfort band to save energy and move heat into cheap hours. Start with 0.2 to 1. |
| Control active | Off = shadow mode. Turning it off resets the offset (offset mode). |
| Retrain model / Recalculate now | Buttons. |
| Model dead time, screed / room time constant, model error, model trained | Diagnostics. A model error (12 h RMSE) below 0.3 K is good. |

The integration runs every hour at hh:07 and after every settings change.
Diagnostics (device page → ⋮ → Download diagnostics) contain the model and the full last plan.

## Watchdog in the heating controller (recommended)

Home Assistant does not unload integrations when it shuts down, so after a crash the last value
stays in place. The set point is written **every hour, even when unchanged**, so the heating
controller can treat it as a heartbeat. ESPHome example for the offset mode:

```yaml
globals:
  - id: offset_last_set
    type: uint32_t
    initial_value: "0"

number:
  - platform: template
    id: fbh_offset
    name: "FBH Vorlauf Heat Temperatur Offset"
    min_value: -5
    max_value: 10
    step: 0.1            # finer than 1 K lets the MPC act precisely
    optimistic: true
    restore_value: true
    set_action:
      - lambda: "id(offset_last_set) = millis();"

interval:
  - interval: 5min
    then:
      - if:
          condition:
            lambda: |-
              return id(fbh_offset).state != 0 &&
                     (millis() - id(offset_last_set)) > 3UL * 3600UL * 1000UL;
          then:
            - number.set:
                id: fbh_offset
                value: 0
```

## Dashboard

The `plan` attribute can be drawn with the
[ApexCharts card](https://github.com/RomRider/apexcharts-card):

```yaml
type: custom:apexcharts-card
header: { show: true, title: Supply plan }
graph_span: 36h
span: { start: hour }
yaxis:
  - id: supply
  - id: room
    opposite: true
series:
  - entity: sensor.fbh_mpc_supply_set_point
    name: Supply plan
    yaxis_id: supply
    data_generator: |
      return entity.attributes.plan.map(p => [new Date(p.time).getTime(), p.supply]);
  - entity: sensor.fbh_mpc_supply_set_point
    name: Room forecast
    yaxis_id: room
    data_generator: |
      return entity.attributes.plan.map(p => [new Date(p.time).getTime() + 3600000, p.room]);
```

Adjust the entity id to yours.

## Limitations and ideas

* One circuit per config entry (add the integration several times for several circuits).
* Data from normal operation with a heating curve contains little independent variation of the
  supply temperature. A few days with manual offset steps of ±2 K make the model more accurate.
* Sun is not an input yet; it shows up as model error. Planned: solar forecast input.
* The energy term uses the supply temperature as a proxy for heat; COP and buffer tanks are not
  modelled.

## Development

```
pip install numpy pytest
cd tests && python make_test_data.py && pytest
```

The numpy core (`model.py`, `mpc.py`, `timeseries.py`) has no Home Assistant imports and is tested
directly; `test_coordinator_stubbed.py` runs the coordinator against a minimal fake Home Assistant.

## License

MIT

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
* Optional: a `weather` entity with an **hourly** forecast (only those are offered), a price
  sensor, e.g. from the [EPEX Spot integration](https://github.com/mampfes/ha_epex_spot).
* Temperatures in °C.

## Setup

**Step 1, measurements:** room sensors of the circuit (the mean is controlled), measured supply
temperature, outdoor temperature (a median of several sensors is ideal), weather entity, price sensor.
Only temperature sensors are offered. For the rooms, the list is narrowed to indoor thermometers
and all of them are preselected; remove the rooms this circuit does not heat.

The price sensor needs a list of hourly prices in an attribute. Detected automatically:
EPEX Spot (`data`: `start_time`/`price_per_kwh`), Nord Pool (`raw_today`/`raw_tomorrow`:
`start`/`value`), Tibber-style (`startsAt`/`total`) and similar.

**Step 2, set point output.** Three ways, the next form only asks for what the chosen way needs:

| Mode | What happens | Next form |
|---|---|---|
| **Own set point sensor** (default) | The integration creates the sensor *Supply set point output*. Your heating controller reads it, e.g. with an ESPHome `homeassistant` sensor. It only has a value while control is active and the model is trustworthy; otherwise it is `unavailable` and the controller keeps its own heating curve. | none |
| **Climate entity** | The set point is written to the target temperature of a climate entity every hour. | climate entity |
| **Number entity** | The set point is written to a `number` / `input_number` every hour. Entities whose maximum is below *Supply minimum* (e.g. an offset) are refused. | number entity |

The integration creates its own switch **Control active**; nothing has to be set up for that. It
starts switched off: the plan is calculated and shown, but nothing is sent. Compare it with your
heating curve for a few days, then switch it on.

In the options you can additionally pick a *heating mode condition* (e.g. the operating mode of the
heating): while it is `off`/`cool`, control pauses automatically.

### Learning

With existing recorder history the model is ready right after setup. Without enough history the
status is *Learning model* and the sensor *Learning progress* counts up to 100 % (7 days of usable
hourly data and a 12 h prediction error below 0.5 K). Until then nothing is sent, and the model is
retrained every hour instead of every night.

## Entities

| Entity | Meaning |
|---|---|
| Supply set point output | Only in the sensor mode: the set point for the heating controller, `unavailable` while control is off. |
| Supply set point calculated | Always: the optimal set point for this hour. Attribute `plan`: hourly plan of supply, predicted room temperature, outdoor and price for the whole horizon. |
| Room temperature mean | Mean of the room sensors right now. |
| Room forecast in 6 h | Predicted room temperature (attributes: in 12 h, min/max next 24 h). |
| Status | Learning model · Ready, control off · Control active · Paused, no heating mode · Measurement missing · Transmission failed |
| Learning progress | 0 to 100 %, see above. |
| Comfort minimum / maximum | Comfort band of the room mean. |
| Supply minimum / maximum | Limits of the supply set point. |
| Max change per hour | Rate limit of the set point. |
| Smoothing weight | Higher = calmer supply temperature, slower reaction. |
| Energy / price weight | 0 = comfort only. Higher = use the comfort band to save energy and move heat into cheap hours. Start with 0.2 to 1. |
| Control active | Switches the output on and off. |
| Retrain model / Recalculate now | Buttons. |
| Model dead time, screed / room time constant, model error, model trained | Diagnostics. A model error (12 h RMSE) below 0.3 K is good. |

The integration runs every hour at hh:07 and after every settings change.
Diagnostics (device page → ⋮ → Download diagnostics) contain the model and the full last plan.

## Reading the set point in ESPHome (sensor mode)

```yaml
sensor:
  - platform: homeassistant
    id: mpc_supply_setpoint
    entity_id: sensor.fbh_mpc_supply_set_point_output   # adjust to your entity id
```

Where your firmware computes the supply set point from the heating curve:

```cpp
float setpoint = curve_setpoint;               // your existing heating curve
if (global_api_server->is_connected() && !std::isnan(id(mpc_supply_setpoint).state)) {
  setpoint = id(mpc_supply_setpoint).state;    // MPC takes over while it delivers a value
}
```

If Home Assistant stops, the API disconnects; if control is switched off or the model is not
trustworthy, the sensor is `unavailable` (NaN). In both cases the heating curve is used again.

In the climate and number modes the set point is written every hour, even when unchanged, so the
heating controller can treat it as a heartbeat and fall back to its own curve after ~3 hours of
silence.

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
  - entity: sensor.fbh_mpc_supply_set_point_calculated
    name: Supply plan
    yaxis_id: supply
    data_generator: |
      return entity.attributes.plan.map(p => [new Date(p.time).getTime(), p.supply]);
  - entity: sensor.fbh_mpc_supply_set_point_calculated
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

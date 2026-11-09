"""Tests for the numpy-only parts (run with: pytest tests)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import math

import numpy as np
import pytest

from conftest_stub import load

M, C = load()
import mpc_floor_heating.timeseries as T  # noqa: E402  (loaded by the stub)


def _simulate_house(true: M.ThermalModel, u: np.ndarray, o: np.ndarray, y0: float, noise: float, seed: int = 1):
    rng = np.random.default_rng(seed)
    n = len(u)
    y = np.empty(n)
    y[0] = y0
    xf = u[0]
    for k in range(n - 1):
        j = k - true.dead_time
        u_in = u[j] if j >= 0 else u[0]
        xf_next = true.lam * xf + (1 - true.lam) * u_in
        y[k + 1] = y[k] + true.a * (xf - y[k]) + true.b * (o[k] - y[k]) + true.c
        xf = xf_next
    return y + rng.normal(0, noise, n)


def _excitation(n: int, seed: int = 2):
    rng = np.random.default_rng(seed)
    hours = np.arange(n)
    o = 4 + 5 * np.sin(2 * np.pi * hours / 24) + np.cumsum(rng.normal(0, 0.15, n))
    u = 31 - 0.4 * (o - 4)
    # occasional offset steps make the model identifiable
    steps = np.repeat(rng.choice([-3.0, 0.0, 3.0], n // 24 + 1), 24)[:n]
    return u + steps, o


def test_identify_recovers_dynamics():
    true = M.ThermalModel(dead_time=4, tau=6.0, a=0.03, b=0.015, c=0.02)
    u, o = _excitation(24 * 40)
    y = _simulate_house(true, u, o, 21.5, noise=0.03)
    model = M.identify(y, u, o)
    assert abs(model.dead_time - true.dead_time) <= 2
    assert model.a == pytest.approx(true.a, rel=0.35)
    assert model.b == pytest.approx(true.b, rel=0.35)
    room_tau_true = 1 / (true.a + true.b)
    assert 1 / (model.a + model.b) == pytest.approx(room_tau_true, rel=0.3)
    assert model.rmse_multi < 0.2


def test_identify_rejects_garbage():
    rng = np.random.default_rng(0)
    with pytest.raises(M.ModelError):
        M.identify(rng.normal(21, 0.1, 20), rng.normal(30, 1, 20), rng.normal(5, 1, 20))


def test_fill_gaps_only_short_runs():
    arr = np.array([1.0, np.nan, 3.0, np.nan, np.nan, np.nan, np.nan, 8.0])
    out = M.fill_gaps(arr, max_gap=3)
    assert out[1] == pytest.approx(2.0)
    assert np.isnan(out[3:7]).all()


def test_floor_state_matches_simulation():
    model = M.ThermalModel(dead_time=2, tau=4.0, a=0.03, b=0.02, c=0.0)
    u_hist = np.linspace(25, 32, 60)
    xf0 = model.floor_state(u_hist)
    assert u_hist[-10] < xf0 < u_hist[-1]


def _mpc_case(outdoor, **kw):
    model = M.ThermalModel(dead_time=3, tau=4.0, a=0.025, b=0.019, c=0.035)
    settings = C.MpcSettings(**kw)
    u_hist = np.full(96, 31.0)
    return model, settings, C.solve(model, settings, 21.8, u_hist, outdoor)


def test_mpc_respects_supply_limits_and_rate():
    model, settings, res = _mpc_case(np.full(36, -10.0), supply_max=34.0, max_step=0.5)
    assert all(settings.supply_min - 1e-6 <= v <= settings.supply_max + 1e-6 for v in res.plan_supply)
    assert abs(res.supply - 31.0) <= 0.5 + 1e-9
    assert res.supply_raw == pytest.approx(34.0, abs=0.05)  # wants max heat


def test_mpc_holds_band_in_closed_loop():
    model = M.ThermalModel(dead_time=3, tau=4.0, a=0.025, b=0.019, c=0.035)
    settings = C.MpcSettings()
    hours = np.arange(400)
    outdoor = 3 + 4 * np.sin(2 * np.pi * hours / 24) - 4 * (hours > 200)  # cold front (still within supply_max)
    u_hist = list(np.full(96, 31.0))
    y, xf = 22.0, 31.0
    ys = []
    for k in range(300):
        res = C.solve(model, settings, y, np.array(u_hist), outdoor[k : k + 36], u_prev=u_hist[-1])
        u_in = u_hist[-model.dead_time]
        xf_next = model.lam * xf + (1 - model.lam) * u_in
        y = y + model.a * (xf - y) + model.b * (outdoor[k] - y) + model.c
        xf = xf_next
        u_hist.append(res.supply)
        ys.append(y)
    ys = np.array(ys[48:])
    assert ys.min() > settings.comfort_min - 0.25
    assert ys.max() < settings.comfort_max + 0.25


def test_mpc_shifts_heat_to_cheap_hours():
    outdoor = np.full(36, 2.0)
    price = np.array([1.0 if 8 <= (h % 24) < 20 else 0.3 for h in range(36)])
    _, _, flat = _mpc_case(outdoor, w_cost=1.0)
    model, settings, res = _mpc_case(outdoor, w_cost=1.0)
    res = C.solve(model, settings, 21.8, np.full(96, 31.0), outdoor, price)
    plan = np.array(res.plan_supply[:24])
    cheap = plan[[h for h in range(24) if not 8 <= h < 20]].mean()
    dear = plan[[h for h in range(24) if 8 <= h < 20]].mean()
    assert cheap > dear


def test_mpc_rejects_missing_room():
    model = M.ThermalModel(dead_time=1, tau=3.0, a=0.02, b=0.02, c=0.0)
    with pytest.raises(ValueError):
        C.solve(model, C.MpcSettings(), math.nan, np.full(48, 30.0), np.full(36, 5.0))


# ------------------------------------------------------------------ timeseries
def test_price_points_epex_format():
    attrs = {
        "unit_of_measurement": "EUR/kWh",
        "data": [
            {"start_time": "2026-11-09T00:00:00+01:00", "end_time": "2026-11-09T01:00:00+01:00", "price_per_kwh": 0.36},
            {"start_time": "2026-11-09T01:00:00+01:00", "end_time": "2026-11-09T02:00:00+01:00", "price_per_kwh": 0.35},
        ],
    }
    pts = T.extract_price_points(attrs)
    assert len(pts) == 2 and pts[0][1] == 0.36
    start = datetime(2026, 11, 8, 23, 0, tzinfo=UTC).timestamp()  # 00:00 local
    series = T.step_series(pts, np.array([start, start + 3600, start + 7200, start + 4 * 3600]))
    assert series[0] == 0.36 and series[1] == 0.35
    assert np.isnan(series[3])


def test_price_points_nordpool_format():
    attrs = {"raw_today": [{"start": datetime(2026, 11, 9, h, tzinfo=UTC), "end": None, "value": h / 10} for h in range(24)]}
    pts = T.extract_price_points(attrs)
    assert len(pts) == 24 and pts[5][1] == 0.5


def test_outdoor_series_bias_fades():
    now = datetime(2026, 11, 9, 12, 5, tzinfo=UTC)
    fc = [((now + timedelta(hours=h)).timestamp(), 5.0) for h in range(-1, 40)]
    slots = np.array([(now.replace(minute=0) + timedelta(hours=k)).timestamp() for k in range(36)])
    out = T.outdoor_series(fc, slots, now.timestamp(), current=7.0)
    assert out[0] == pytest.approx(7.0, abs=0.3)
    assert out[-1] == pytest.approx(5.0, abs=0.05)


def test_outdoor_series_persistence_without_forecast():
    out = T.outdoor_series([], np.arange(5) * 3600.0, 0.0, current=3.3)
    assert (out == 3.3).all()
    with pytest.raises(ValueError):
        T.outdoor_series([], np.arange(5) * 3600.0, 0.0, current=None)


def test_hourly_mean_from_points():
    start = 0.0
    pts = [(-100.0, 20.0), (1800.0, 22.0), (3600.0, math.nan), (5400.0, 24.0)]
    out = T.hourly_mean_from_points(pts, start, 3)
    assert out[0] == pytest.approx(21.0)
    assert out[1] == pytest.approx(24.0)  # only the valid half counts
    assert out[2] == pytest.approx(24.0)


def test_fill_edges_uses_live_value():
    arr = np.array([20.0, np.nan, 22.0, np.nan])
    out = T.fill_edges(arr, last_value=24.0)
    assert out[1] == pytest.approx(21.0)
    assert out[3] == pytest.approx(23.0)


def test_naive_times_use_default_zone():
    from datetime import timezone
    tz = timezone(timedelta(hours=1))
    assert T.to_timestamp("2026-11-09T15:00:00") is None
    assert T.to_timestamp("2026-11-09T15:00:00", None, tz) == datetime(2026, 11, 9, 14, 0, tzinfo=UTC).timestamp()

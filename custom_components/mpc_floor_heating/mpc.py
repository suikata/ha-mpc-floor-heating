"""Model predictive controller for the supply temperature (pure numpy).

The model is linear, so the predicted room temperature is

    y = y_free + G @ v

where v are the hourly supply set points that are still free to choose.
Set points after hour M are held at v[M-1] (move blocking), because a change
later than "horizon - dead time" no longer shows up inside the horizon.

Cost (all terms convex):

    sum_k  w_band  * (max(0, lo - y_k)^2 + max(0, y_k - hi)^2)   comfort band
         + w_ref   * (y_k - ref)^2                               stay near the middle
    + w_move * sum_k (u_k - u_{k-1})^2                           calm supply
    + w_cost * sum_k p_k * u_k                                   energy / price

solved with accelerated projected gradient (FISTA) under lo_u <= u <= hi_u.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math

import numpy as np

from .model import ThermalModel


@dataclass
class MpcSettings:
    """Tunable settings, all exposed as number entities in Home Assistant."""

    tolerance: float = 0.5  # K a room may deviate from its set point
    default_setpoint: float = 22.0  # for rooms without a thermostat
    supply_min: float = 22.0
    supply_max: float = 38.0
    max_step: float = 1.0  # K per hour applied to the first move
    w_move: float = 1.0
    w_cost: float = 0.0
    w_band: float = 20.0
    w_ref: float = 1.0
    horizon: int = 36


@dataclass
class MpcResult:
    """Outcome of one optimisation."""

    supply: float  # set point to apply now (rate limited)
    supply_raw: float  # first move before rate limiting
    plan_supply: list[float] = field(default_factory=list)  # hours 0..H-1
    plan_room: list[float] = field(default_factory=list)  # hours 1..H
    room_now: float = math.nan
    iterations: int = 0
    cost: float = math.nan
    steady_state_supply: float = math.nan
    worst_room: list[float] = field(default_factory=list)  # hours 1..H, largest shortfall


@dataclass
class Room:
    """A room relative to the modelled mean temperature."""

    offset: float  # room temperature minus the modelled mean, assumed to persist
    setpoint: float


def _fill_nan(arr: np.ndarray, what: str) -> np.ndarray:
    """Interpolate inner NaNs and extend the edges; fail if nothing is valid."""
    good = np.isfinite(arr)
    if not good.any():
        raise ValueError(f"{what} contains no valid values")
    if good.all():
        return arr
    idx = np.arange(len(arr))
    return np.interp(idx, idx[good], arr[good])


def _blocking_matrix(horizon: int, free: int) -> np.ndarray:
    """Map the free moves v (len free) to the full plan u (len horizon)."""
    mat = np.zeros((horizon, free))
    for k in range(horizon):
        mat[k, min(k, free - 1)] = 1.0
    return mat


def solve(
    model: ThermalModel,
    settings: MpcSettings,
    room_now: float,
    u_hist: np.ndarray,
    outdoor: np.ndarray,
    price: np.ndarray | None = None,
    u_prev: float | None = None,
    warm_start: np.ndarray | None = None,
    rooms: list[Room] | None = None,
    max_iter: int = 4000,
    tol: float = 1e-7,
) -> MpcResult:
    """Compute the optimal supply plan.

    u_hist: hourly supply means, oldest first, last = previous complete hour
    outdoor: outdoor forecast for hours 0 .. H-1 (len >= horizon)
    price: optional price per hour 0 .. H-1, any unit (normalised to mean 1)
    u_prev: supply set point currently active (reference for move penalty)
    rooms: rooms with their own set points. Each room follows the modelled
        mean plus its current offset; the valves of rooms above their set
        point throttle by themselves, so only shortfalls are penalised per
        room, while the upper limit applies to the mean. Without rooms the
        mean has to stay at default_setpoint +- tolerance.
    """
    horizon = int(settings.horizon)
    outdoor = _fill_nan(np.asarray(outdoor, dtype=float)[:horizon], "outdoor forecast")
    if len(outdoor) < horizon:
        outdoor = np.concatenate([outdoor, np.full(horizon - len(outdoor), outdoor[-1])])
    u_hist = _fill_nan(np.asarray(u_hist, dtype=float), "supply history")
    if not math.isfinite(room_now):
        raise ValueError("room temperature is not a number")
    if u_prev is None or not math.isfinite(u_prev):
        u_prev = float(u_hist[-1])

    lo_u = float(settings.supply_min)
    hi_u = float(max(settings.supply_max, lo_u))
    free = max(1, horizon - model.dead_time - 1)
    block = _blocking_matrix(horizon, free)

    xf0 = model.floor_state(u_hist)
    zero_plan = np.zeros(horizon)
    y_free, _ = model.simulate(room_now, xf0, u_hist, zero_plan, outdoor)
    y_free = y_free[1:]
    # impulse responses of the free moves
    gain = np.zeros((horizon, free))
    for j in range(free):
        y_j, _ = model.simulate(room_now, xf0, u_hist, block[:, j], outdoor)
        gain[:, j] = y_j[1:] - y_free

    tol_k = max(0.0, float(settings.tolerance))
    if not rooms:
        rooms = [Room(0.0, float(settings.default_setpoint))]
    offsets = np.array([r.offset for r in rooms], dtype=float)
    floors = np.array([r.setpoint - tol_k for r in rooms], dtype=float) - offsets  # min mean per room
    mean_sp = float(np.mean([r.setpoint for r in rooms])) - float(np.mean(offsets))
    hi_y = mean_sp + tol_k
    # aim at the level where the neediest room reaches its set point, but do
    # not overheat the whole house for one room that cannot keep up
    need = float(np.max(floors)) + tol_k
    ref = min(need, hi_y)

    # move penalty on the full plan, including the step from u_prev
    diff = np.eye(horizon) - np.eye(horizon, k=-1)
    d_mat = diff @ block
    d_off = np.zeros(horizon)
    d_off[0] = -u_prev

    if price is None:
        p = np.ones(horizon)
    else:
        p = np.asarray(price, dtype=float)[:horizon]
        if len(p) < horizon:
            fill = np.nanmean(p) if len(p) and not np.all(np.isnan(p)) else 1.0
            p = np.concatenate([p, np.full(horizon - len(p), fill)])
        mean = np.nanmean(p) if not np.all(np.isnan(p)) else 1.0
        p = np.where(np.isnan(p), mean, p)
        # scale by the mean magnitude so negative prices keep their sign
        scale = float(np.mean(np.abs(p)))
        p = p / scale if scale > 1e-9 else np.ones(horizon)
    lin_cost = settings.w_cost * (block.T @ p)

    w_band, w_ref, w_move = settings.w_band, settings.w_ref, settings.w_move

    def cost_and_grad(v: np.ndarray) -> tuple[float, np.ndarray]:
        y = y_free + gain @ v
        below = np.maximum(0.0, floors[:, None] - y[None, :])  # rooms x hours
        above = np.maximum(0.0, y - hi_y)
        dev = y - ref
        mv = d_mat @ v + d_off
        cost = (
            w_band * float(np.sum(below * below) + above @ above)
            + w_ref * float(dev @ dev)
            + w_move * float(mv @ mv)
            + float(lin_cost @ v)
        )
        dy = 2.0 * w_band * (above - below.sum(axis=0)) + 2.0 * w_ref * dev
        grad = gain.T @ dy + 2.0 * w_move * (d_mat.T @ mv) + lin_cost
        return cost, grad

    # Lipschitz constant of the gradient
    sigma_g = np.linalg.norm(gain, 2) if gain.size else 0.0
    sigma_d = np.linalg.norm(d_mat, 2)
    lipschitz = 2.0 * (w_band * (len(rooms) + 1) + w_ref) * sigma_g**2 + 2.0 * w_move * sigma_d**2 + 1e-9
    step = 1.0 / lipschitz

    if warm_start is not None and len(warm_start) >= free:
        v = np.clip(np.asarray(warm_start, dtype=float)[:free], lo_u, hi_u)
    else:
        start = model.supply_for_room(ref, float(np.mean(outdoor)))
        if not math.isfinite(start):
            start = u_prev
        v = np.full(free, float(np.clip(start, lo_u, hi_u)))

    z = v.copy()
    t = 1.0
    it = 0
    for it in range(1, max_iter + 1):
        _, grad = cost_and_grad(z)
        v_new = np.clip(z - step * grad, lo_u, hi_u)
        t_new = 0.5 * (1.0 + math.sqrt(1.0 + 4.0 * t * t))
        z = v_new + ((t - 1.0) / t_new) * (v_new - v)
        moved = float(np.max(np.abs(v_new - v)))
        v, t = v_new, t_new
        if moved < tol:
            break

    cost, _ = cost_and_grad(v)
    plan = block @ v
    y_plan = y_free + gain @ v
    first = float(plan[0])
    applied = float(np.clip(first, u_prev - settings.max_step, u_prev + settings.max_step))
    applied = float(np.clip(applied, lo_u, hi_u))
    return MpcResult(
        supply=applied,
        supply_raw=first,
        plan_supply=[float(x) for x in plan],
        plan_room=[float(x) for x in y_plan],
        room_now=float(room_now),
        iterations=it,
        cost=float(cost),
        steady_state_supply=float(model.supply_for_room(ref, float(outdoor[0]))),
        # largest shortfall below a room set point (negative = below), per hour
        worst_room=[float(x) for x in np.min(y_plan[None, :] - (floors[:, None] + tol_k), axis=0)],
    )

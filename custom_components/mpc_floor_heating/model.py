"""Grey-box thermal model of a floor-heated building (pure numpy, no HA imports).

Hourly discrete model with two states:

    screed / floor (not measured):
        xf[k+1] = lam * xf[k] + (1 - lam) * u[k - d]          lam = exp(-1 / tau)

    room air (measured, average of the selected room sensors):
        y[k+1] = y[k] + a * (xf[k] - y[k]) + b * (o[k] - y[k]) + c

    u  supply (flow) temperature of the floor heating circuit   [degC]
    o  outdoor temperature                                     [degC]
    d  dead time in hours, tau screed time constant in hours
    a  heat transfer floor -> room per hour, b room -> outside per hour
    c  constant gains (internal + average solar) in K per hour

d and tau are found by grid search, a, b and c by linear least squares.
The grid point with the smallest multi-step (open loop) prediction error wins,
because that is what the MPC relies on.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math

import numpy as np

DEAD_TIMES = tuple(range(0, 13))  # hours
TIME_CONSTANTS = (1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0, 24.0, 36.0)  # hours
EVAL_STEPS = 12  # open loop horizon used to score a candidate model
MAX_GAP_HOURS = 3  # gaps up to this length are interpolated
MIN_ROWS = 72  # need at least 3 days of usable hours


class ModelError(Exception):
    """Raised when no usable model can be identified."""


@dataclass
class ThermalModel:
    """Identified model parameters."""

    dead_time: int
    tau: float
    a: float
    b: float
    c: float
    rmse_1h: float = math.nan
    rmse_multi: float = math.nan
    rows: int = 0
    trained_at: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def lam(self) -> float:
        return math.exp(-1.0 / self.tau)

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ThermalModel:
        keys = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in data.items() if k in keys})

    def steady_state_room(self, u: float, o: float) -> float:
        """Room temperature the model settles at for constant u and o."""
        return (self.a * u + self.b * o + self.c) / (self.a + self.b)

    def supply_for_room(self, y: float, o: float) -> float:
        """Inverse of steady_state_room: supply needed for room temp y."""
        return ((self.a + self.b) * y - self.b * o - self.c) / self.a

    # ------------------------------------------------------------------ state
    def floor_state(self, u_hist: np.ndarray) -> float:
        """Run the screed filter over the supply history.

        u_hist holds hourly supply means, oldest first, the last element is
        the most recent complete hour (index -1). Returns xf for hour 0
        (the current, still running hour). NaNs must already be filled.
        """
        u_hist = np.asarray(u_hist, dtype=float)
        d = self.dead_time
        lam = self.lam
        n = len(u_hist)
        if n <= d:
            raise ModelError("supply history shorter than dead time")
        # inputs that drove xf: u[k-d] for k = d .. n-1
        drive = u_hist[: n - d]
        xf = drive[0]
        for val in drive:
            xf = lam * xf + (1.0 - lam) * val
        return float(xf)

    def simulate(
        self,
        y0: float,
        xf0: float,
        u_hist: np.ndarray,
        u_plan: np.ndarray,
        o_plan: np.ndarray,
        bias: np.ndarray | float = 0.0,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Predict room and floor temperature.

        u_hist: past hourly supply (oldest first, last = hour -1)
        u_plan: supply for hours 0 .. H-1
        o_plan: outdoor temperature for hours 0 .. H-1
        Returns (y, xf), each of length H + 1, index 0 = now.
        """
        u_plan = np.asarray(u_plan, dtype=float)
        o_plan = np.asarray(o_plan, dtype=float)
        horizon = len(u_plan)
        bias = np.broadcast_to(np.asarray(bias, dtype=float), (horizon,))
        d = self.dead_time
        lam = self.lam
        y = np.empty(horizon + 1)
        xf = np.empty(horizon + 1)
        y[0] = y0
        xf[0] = xf0
        for k in range(horizon):
            j = k - d
            u_in = u_hist[j] if j < 0 else u_plan[j]
            xf[k + 1] = lam * xf[k] + (1.0 - lam) * u_in
            y[k + 1] = (
                y[k]
                + self.a * (xf[k] - y[k])
                + self.b * (o_plan[k] - y[k])
                + self.c
                + bias[k]
            )
        return y, xf

    def one_step_residuals(
        self, y_hist: np.ndarray, u_hist: np.ndarray, o_hist: np.ndarray
    ) -> np.ndarray:
        """Measured minus predicted one-step change over the history."""
        y = np.asarray(y_hist, dtype=float)
        o = np.asarray(o_hist, dtype=float)
        xf = _floor_series(np.asarray(u_hist, dtype=float), self.dead_time, self.lam)
        pred = y[:-1] + self.a * (xf[:-1] - y[:-1]) + self.b * (o[:-1] - y[:-1]) + self.c
        return y[1:] - pred


# ---------------------------------------------------------------------- helpers
def fill_gaps(values: np.ndarray, max_gap: int = MAX_GAP_HOURS) -> np.ndarray:
    """Linearly interpolate NaN runs up to max_gap, keep longer gaps as NaN."""
    arr = np.asarray(values, dtype=float).copy()
    isnan = np.isnan(arr)
    if not isnan.any() or isnan.all():
        return arr
    idx = np.arange(len(arr))
    good = ~isnan
    filled = np.interp(idx, idx[good], arr[good])
    # find NaN runs and only accept short interior ones
    run_start = None
    for i in range(len(arr) + 1):
        nan_here = i < len(arr) and isnan[i]
        if nan_here and run_start is None:
            run_start = i
        elif not nan_here and run_start is not None:
            length = i - run_start
            interior = run_start > 0 and i < len(arr)
            if interior and length <= max_gap:
                arr[run_start:i] = filled[run_start:i]
            run_start = None
    return arr


def _floor_series(u: np.ndarray, d: int, lam: float) -> np.ndarray:
    """xf[k] for every k, NaN where it cannot be computed (gaps / warm-up)."""
    n = len(u)
    xf = np.full(n, np.nan)
    warmup = 0
    state = math.nan
    warm_needed = max(1, int(round(3.0 / max(1e-6, -math.log(lam)))))  # 3 tau
    for k in range(n - 1):
        j = k - d
        if j < 0:
            continue
        u_in = u[j]
        if math.isnan(u_in):
            state = math.nan
            warmup = 0
            continue
        if math.isnan(state):
            state = u_in
            warmup = 0
        state = lam * state + (1.0 - lam) * u_in
        warmup += 1
        if warmup >= warm_needed:
            xf[k + 1] = state
    return xf


def _fit_abc(y, o, xf, rows):
    """Least squares for a, b, c on the given rows. Returns (a, b, c)."""
    dy = y[rows + 1] - y[rows]
    x1 = xf[rows] - y[rows]
    x2 = o[rows] - y[rows]
    mat = np.column_stack([x1, x2, np.ones_like(x1)])
    coef, *_ = np.linalg.lstsq(mat, dy, rcond=None)
    a, b, c = (float(v) for v in coef)
    if b < 0.0:  # outdoor losses must not be negative: fix b = 0 and refit
        coef, *_ = np.linalg.lstsq(mat[:, [0, 2]], dy, rcond=None)
        a, c = (float(v) for v in coef)
        b = 0.0
    return a, b, c


def _multi_step_rmse(y, o, xf, rows, a, b, c, steps):
    """Open-loop RMSE over `steps` hours, started at every row in parallel."""
    n = len(y)
    starts = rows[rows + steps < n]
    if len(starts) == 0:
        return math.inf
    yy = y[starts].copy()
    errs = []
    for s in range(steps):
        k = starts + s
        xk = xf[k]
        ok = ~np.isnan(xk) & ~np.isnan(o[k]) & ~np.isnan(y[k + 1])
        yy = yy + a * (xk - yy) + b * (o[k] - yy) + c
        err = (yy - y[k + 1])[ok]
        errs.append(err)
    all_err = np.concatenate(errs)
    all_err = all_err[~np.isnan(all_err)]
    if len(all_err) == 0:
        return math.inf
    return float(np.sqrt(np.mean(all_err**2)))


def identify(
    y_hist: np.ndarray,
    u_hist: np.ndarray,
    o_hist: np.ndarray,
    dead_times: tuple[int, ...] = DEAD_TIMES,
    time_constants: tuple[float, ...] = TIME_CONSTANTS,
    eval_steps: int = EVAL_STEPS,
) -> ThermalModel:
    """Identify the model from hourly means (oldest first, equal length)."""
    y = fill_gaps(np.asarray(y_hist, dtype=float))
    u = fill_gaps(np.asarray(u_hist, dtype=float))
    o = fill_gaps(np.asarray(o_hist, dtype=float))
    if not len(y) == len(u) == len(o):
        raise ModelError("history arrays have different lengths")

    best: ThermalModel | None = None
    candidates = []
    for tau in time_constants:
        lam = math.exp(-1.0 / tau)
        for d in dead_times:
            xf = _floor_series(u, d, lam)
            k = np.arange(len(y) - 1)
            ok = ~(np.isnan(xf[k]) | np.isnan(y[k]) | np.isnan(y[k + 1]) | np.isnan(o[k]))
            rows = k[ok]
            if len(rows) < MIN_ROWS:
                continue
            a, b, c = _fit_abc(y, o, xf, rows)
            if not 0.0 < a < 1.0 or not 0.0 <= b < 1.0:
                continue
            resid = (y[rows + 1] - y[rows]) - (
                a * (xf[rows] - y[rows]) + b * (o[rows] - y[rows]) + c
            )
            rmse1 = float(np.sqrt(np.mean(resid**2)))
            rmse_m = _multi_step_rmse(y, o, xf, rows, a, b, c, eval_steps)
            candidates.append((rmse_m, d, tau))
            if best is None or rmse_m < best.rmse_multi:
                best = ThermalModel(
                    dead_time=d,
                    tau=tau,
                    a=a,
                    b=b,
                    c=c,
                    rmse_1h=rmse1,
                    rmse_multi=rmse_m,
                    rows=int(len(rows)),
                )
    if best is None:
        raise ModelError(
            "no physically plausible model found - more data or more variation "
            "in the supply temperature is needed"
        )
    # how sharp is the optimum? (useful for diagnostics)
    candidates.sort()
    best.extra = {
        "runner_up": [
            {"rmse": round(r, 4), "dead_time": d, "tau": t} for r, d, t in candidates[1:4]
        ],
        "candidates": len(candidates),
    }
    return best
